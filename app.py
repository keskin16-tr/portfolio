from __future__ import annotations

import io
import os
import sqlite3
import time
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime, timezone

import pandas as pd
import requests
from flask import Flask, jsonify, render_template, request, send_file

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("PORTFOLIO_DB") or os.path.join(APP_DIR, "portfolio.db")
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024
app.config["JSON_AS_ASCII"] = False

OZ_GRAM = 31.1034768  # 1 troy ons = 31.1034768 gram


# ----------------------------------------------------------------------------
# Veritabanı
# ----------------------------------------------------------------------------
@contextmanager
def db():
    con = sqlite3.connect(DB_PATH, timeout=45, isolation_level="DEFERRED")
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=45000")
        con.execute("PRAGMA synchronous=NORMAL")
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def columns(con, table):
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def init_db():
    bootstrap = sqlite3.connect(DB_PATH, timeout=45)
    try:
        bootstrap.execute("PRAGMA journal_mode=WAL")
        bootstrap.execute("PRAGMA busy_timeout=45000")
    finally:
        bootstrap.close()
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, symbol TEXT DEFAULT '',
            category TEXT NOT NULL DEFAULT 'Diğer', account TEXT DEFAULT '', quantity REAL NOT NULL DEFAULT 0,
            unit_price REAL NOT NULL DEFAULT 0, currency TEXT NOT NULL DEFAULT 'TRY',
            cost_per_unit REAL NOT NULL DEFAULT 0, notes TEXT DEFAULT '', created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, price_source TEXT DEFAULT 'manual', market_symbol TEXT DEFAULT ''
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, asset_id INTEGER, asset_name TEXT NOT NULL,
            transaction_type TEXT NOT NULL, quantity REAL NOT NULL DEFAULT 0, unit_price REAL NOT NULL DEFAULT 0,
            currency TEXT NOT NULL DEFAULT 'TRY', transaction_date TEXT NOT NULL, notes TEXT DEFAULT '',
            created_at TEXT NOT NULL, applied INTEGER NOT NULL DEFAULT 0,
            realized_pnl REAL NOT NULL DEFAULT 0, realized_pnl_try REAL NOT NULL DEFAULT 0,
            FOREIGN KEY(asset_id) REFERENCES assets(id) ON DELETE SET NULL
        )""")
        con.execute("CREATE TABLE IF NOT EXISTS fx_rates(currency TEXT PRIMARY KEY, try_rate REAL NOT NULL, updated_at TEXT DEFAULT '')")
        con.execute("""CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, snapshot_date TEXT NOT NULL UNIQUE,
            total_try REAL NOT NULL, total_cost_try REAL DEFAULT 0, realized_pnl_try REAL DEFAULT 0
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS dividend_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, asset_id INTEGER NOT NULL, event_date TEXT NOT NULL,
            amount_per_share REAL NOT NULL, currency TEXT NOT NULL DEFAULT 'TRY', source TEXT DEFAULT 'Yahoo Finance',
            UNIQUE(asset_id,event_date,amount_per_share), FOREIGN KEY(asset_id) REFERENCES assets(id) ON DELETE CASCADE
        )""")

        migrations = {
            "assets": {"price_source": "TEXT DEFAULT 'manual'", "market_symbol": "TEXT DEFAULT ''"},
            "transactions": {"applied": "INTEGER NOT NULL DEFAULT 0", "realized_pnl": "REAL NOT NULL DEFAULT 0", "realized_pnl_try": "REAL NOT NULL DEFAULT 0"},
            "fx_rates": {"updated_at": "TEXT DEFAULT ''"},
            "snapshots": {"total_cost_try": "REAL DEFAULT 0", "realized_pnl_try": "REAL DEFAULT 0"},
        }
        for table, defs in migrations.items():
            existing = columns(con, table)
            for name, definition in defs.items():
                if name not in existing:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

        con.executemany("INSERT OR IGNORE INTO fx_rates(currency,try_rate,updated_at) VALUES(?,?,?)", [
            ("TRY", 1, ""), ("USD", 34.5, ""), ("EUR", 37.2, ""), ("GBP", 44, ""), ("USDT", 34.5, ""),
            ("GAUTRY", 3050.0, ""), ("GAGTRY", 35.5, "")
        ])


def fx_map(con):
    return {r["currency"]: float(r["try_rate"]) for r in con.execute("SELECT * FROM fx_rates")}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


_TR_MAP = str.maketrans({
    "İ": "i", "I": "i", "ı": "i", "Ş": "s", "ş": "s", "Ğ": "g", "ğ": "g",
    "Ü": "u", "ü": "u", "Ö": "o", "ö": "o", "Ç": "c", "ç": "c",
})


def _norm(value) -> str:
    """Türkçe karakterleri sadeleştirip küçük harfe çevirir (kategori eşleştirmek için)."""
    return str(value or "").translate(_TR_MAP).lower().strip()


def asset_dict(row, rates):
    a = dict(row)
    rate = rates.get(a["currency"], 1.0)
    a["value_native"] = float(a["quantity"] or 0) * float(a["unit_price"] or 0)
    a["cost_native"] = float(a["quantity"] or 0) * float(a["cost_per_unit"] or 0)
    a["value_try"] = a["value_native"] * rate
    a["cost_try"] = a["cost_native"] * rate
    a["profit_try"] = a["value_try"] - a["cost_try"]
    a["profit_pct"] = a["profit_try"] / a["cost_try"] * 100 if a["cost_try"] else 0
    return a


def portfolio_totals(con):
    rates = fx_map(con)
    assets = [asset_dict(r, rates) for r in con.execute("SELECT * FROM assets ORDER BY category,name")]
    total = sum(a["value_try"] for a in assets)
    cost = sum(a["cost_try"] for a in assets)
    realized = float(con.execute("SELECT COALESCE(SUM(realized_pnl_try),0) FROM transactions").fetchone()[0])
    return assets, rates, total, cost, realized


def save_snapshot(con):
    assets, rates, total, cost, realized = portfolio_totals(con)
    today = date.today().isoformat()
    con.execute("""INSERT INTO snapshots(snapshot_date,total_try,total_cost_try,realized_pnl_try)
        VALUES(?,?,?,?) ON CONFLICT(snapshot_date) DO UPDATE SET
        total_try=excluded.total_try,total_cost_try=excluded.total_cost_try,
        realized_pnl_try=excluded.realized_pnl_try""", (today, total, cost, realized))


# ----------------------------------------------------------------------------
# Sayfa rotaları
# ----------------------------------------------------------------------------
@app.get("/")
def page_dashboard():
    return render_template("dashboard.html", active_page="dashboard")

@app.get("/crypto")
def page_crypto():
    return render_template("crypto.html", active_page="crypto")

@app.get("/equities")
def page_equities():
    return render_template("equities.html", active_page="equities")

@app.get("/brokers")
def page_brokers():
    return render_template("brokers.html", active_page="brokers")

@app.get("/analytics")
def page_analytics():
    return render_template("analytics.html", active_page="analytics")

@app.get("/manage")
def page_manage():
    return render_template("manage.html", active_page="manage")


# ----------------------------------------------------------------------------
# Portföy API
# ----------------------------------------------------------------------------
@app.get("/api/dashboard")
def dashboard():
    with db() as con:
        assets, rates, total, cost, realized = portfolio_totals(con)
        categories, accounts, exposure = {}, {}, {}
        for a in assets:
            categories[a["category"]] = categories.get(a["category"], 0) + a["value_try"]
            cur = (a.get("currency") or "TRY").upper()
            exposure[cur] = exposure.get(cur, 0) + a["value_try"]
            acc_name = (a.get("account") or "").strip() or "Belirtilmemiş Hesap"
            if acc_name not in accounts:
                accounts[acc_name] = {"name": acc_name, "value": 0.0, "cost": 0.0, "count": 0}
            accounts[acc_name]["value"] += a["value_try"]
            accounts[acc_name]["cost"] += a["cost_try"]
            accounts[acc_name]["count"] += 1

        account_list = []
        for acc in accounts.values():
            acc["profit"] = acc["value"] - acc["cost"]
            acc["profit_pct"] = (acc["profit"] / acc["cost"] * 100) if acc["cost"] else 0.0
            acc["share_pct"] = (acc["value"] / total * 100) if total else 0.0
            account_list.append(acc)
        account_list.sort(key=lambda x: x["value"], reverse=True)

        tx = [dict(r) for r in con.execute("SELECT * FROM transactions ORDER BY transaction_date DESC,id DESC LIMIT 30")]
        history = [dict(r) for r in con.execute("SELECT * FROM snapshots ORDER BY snapshot_date")]
        dividends = [dict(r) for r in con.execute("SELECT asset_name, SUM(realized_pnl_try) as total_div FROM transactions WHERE transaction_type='Temettü' GROUP BY asset_name")]
        monthly = [dict(r) for r in con.execute("""SELECT substr(transaction_date,1,7) AS month, SUM(realized_pnl_try) AS realized
            FROM transactions WHERE realized_pnl_try!=0 GROUP BY month ORDER BY month""")]
        rates_updated = con.execute("SELECT MAX(updated_at) FROM fx_rates WHERE updated_at!=''").fetchone()[0]

        return jsonify(
            assets=assets, rates=rates, total_try=total, cost_try=cost,
            profit_try=total - cost, profit_pct=((total - cost) / cost * 100 if cost else 0),
            unrealized_pnl_try=total - cost, realized_pnl_try=realized, total_pnl_try=(total - cost + realized),
            categories=[{"name": k, "value": v} for k, v in sorted(categories.items(), key=lambda x: x[1], reverse=True)],
            exposure=[{"name": k, "value": v} for k, v in sorted(exposure.items(), key=lambda x: x[1], reverse=True)],
            accounts=account_list, dividends=dividends, monthly_realized=monthly,
            transactions=tx, history=history, asset_count=len(assets), rates_updated_at=rates_updated,
        )


@app.get("/api/breakdown")
def breakdown():
    with db() as con:
        assets, rates, total, cost, realized = portfolio_totals(con)

    crypto_assets = [a for a in assets if _asset_kind(a) == "crypto"]
    equity_keys = ("hisse", "borsa", "bist", "fon", "etf", "abd", "stock", "equity")
    equity_assets = [a for a in assets if any(k in _norm(a.get("category")) for k in equity_keys)]

    def summarize(group):
        val = sum(x["value_try"] for x in group)
        cst = sum(x["cost_try"] for x in group)
        pnl = val - cst
        return {
            "count": len(group), "value_try": val, "cost_try": cst, "profit_try": pnl,
            "profit_pct": (pnl / cst * 100) if cst else 0.0, "portfolio_share_pct": (val / total * 100) if total else 0.0,
            "assets": sorted(group, key=lambda x: x["value_try"], reverse=True),
        }

    return jsonify(crypto=summarize(crypto_assets), equities=summarize(equity_assets), total_try=total)


# ----------------------------------------------------------------------------
# Varlıklar
# ----------------------------------------------------------------------------
def _asset_payload(con, d):
    name = str(d.get("name", "")).strip()
    category = str(d.get("category", "")).strip()
    if not name or not category:
        raise ValueError("Varlık adı ve kategorisi zorunludur.")
    try:
        qty = float(d.get("quantity") or 0)
        price = float(d.get("unit_price") or 0)
        cost = float(d.get("cost_per_unit") or 0)
    except (TypeError, ValueError):
        raise ValueError("Miktar, birim fiyat ve maliyet sayısal olmalıdır.")
    if qty < 0 or price < 0 or cost < 0:
        raise ValueError("Miktar, birim fiyat ve maliyet negatif olamaz.")
    currency = str(d.get("currency") or "TRY").strip().upper()
    if currency not in fx_map(con):
        raise ValueError(f"'{currency}' için kur tanımlı değil. Önce Kurlar sekmesinden ekleyin.")
    return {
        "name": name, "symbol": str(d.get("symbol", "")).strip(), "category": category,
        "account": str(d.get("account", "")).strip(), "quantity": qty, "unit_price": price,
        "currency": currency, "cost_per_unit": cost, "notes": str(d.get("notes", "")),
        "price_source": str(d.get("price_source") or "manual").strip() or "manual",
        "market_symbol": str(d.get("market_symbol") or "").strip(),
    }


@app.get("/api/assets")
def list_assets():
    with db() as con:
        rates = fx_map(con)
        assets = [asset_dict(r, rates) for r in con.execute("SELECT * FROM assets ORDER BY category,name")]
    return jsonify(assets=assets, rates=rates)


@app.post("/api/assets")
def add_asset():
    data = request.get_json(force=True)
    try:
        with db() as con:
            p = _asset_payload(con, data)
            now = _now()
            cur = con.execute("""INSERT INTO assets(name,symbol,category,account,quantity,unit_price,currency,cost_per_unit,
                notes,created_at,updated_at,price_source,market_symbol) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                p["name"], p["symbol"], p["category"], p["account"], p["quantity"], p["unit_price"], p["currency"],
                p["cost_per_unit"], p["notes"], now, now, p["price_source"], p["market_symbol"]))
            save_snapshot(con)
        return jsonify(ok=True, id=cur.lastrowid)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400


@app.route("/api/assets/<int:asset_id>", methods=["PUT", "DELETE"])
def modify_asset(asset_id):
    try:
        with db() as con:
            if not con.execute("SELECT id FROM assets WHERE id=?", (asset_id,)).fetchone():
                return jsonify(error="Varlık bulunamadı."), 404
            if request.method == "DELETE":
                con.execute("DELETE FROM assets WHERE id=?", (asset_id,))
                save_snapshot(con)
                return jsonify(ok=True)
            p = _asset_payload(con, request.get_json(force=True))
            con.execute("""UPDATE assets SET name=?,symbol=?,category=?,account=?,quantity=?,unit_price=?,currency=?,
                cost_per_unit=?,notes=?,updated_at=?,price_source=?,market_symbol=? WHERE id=?""", (
                p["name"], p["symbol"], p["category"], p["account"], p["quantity"], p["unit_price"], p["currency"],
                p["cost_per_unit"], p["notes"], _now(), p["price_source"], p["market_symbol"], asset_id))
            save_snapshot(con)
        return jsonify(ok=True)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400


# ----------------------------------------------------------------------------
# İşlemler
# ----------------------------------------------------------------------------
TX_TYPES = {"Alış", "Satış", "Temettü", "Para girişi", "Para çıkışı", "Transfer"}


class TxError(ValueError):
    pass


def _tx_payload(d):
    kind = str(d.get("transaction_type", "")).strip()
    if kind not in TX_TYPES:
        raise TxError("Geçersiz işlem türü.")
    try:
        qty = float(d.get("quantity") or 0)
        price = float(d.get("unit_price") or 0)
    except (TypeError, ValueError):
        raise TxError("Miktar ve fiyat sayısal olmalıdır.")
    if qty < 0 or price < 0:
        raise TxError("Miktar ve fiyat negatif olamaz.")
    name = str(d.get("asset_name", "")).strip()
    if not name:
        raise TxError("Varlık adı zorunludur.")
    tdate = str(d.get("transaction_date") or date.today().isoformat())[:10]
    try:
        date.fromisoformat(tdate)
    except ValueError:
        raise TxError("Tarih geçersiz (YYYY-AA-GG olmalı).")
    try:
        asset_id = int(d.get("asset_id")) if d.get("asset_id") not in (None, "") else None
    except (TypeError, ValueError):
        asset_id = None
    return {
        "kind": kind, "qty": qty, "price": price, "name": name, "date": tdate,
        "currency": str(d.get("currency") or "").strip().upper(),
        "notes": str(d.get("notes", "")), "asset_id": asset_id,
    }


def _find_asset(con, asset_id, name):
    if asset_id:
        row = con.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
        if row:
            return row
    return con.execute("SELECT * FROM assets WHERE name=? COLLATE NOCASE ORDER BY id LIMIT 1", (name,)).fetchone()


def _apply_tx(con, asset, kind, qty, price, currency, rates):
    """Alış/Satış'ı varlık bakiyesine uygular. (realized, realized_try) döner."""
    old_qty = float(asset["quantity"])
    old_cost = float(asset["cost_per_unit"])
    if kind == "Alış":
        new_qty = old_qty + qty
        new_cost = ((old_qty * old_cost) + (qty * price)) / new_qty if new_qty else 0.0
        con.execute("UPDATE assets SET quantity=?,cost_per_unit=?,updated_at=? WHERE id=?",
                    (new_qty, new_cost, _now(), asset["id"]))
        return 0.0, 0.0
    if qty > old_qty + 1e-9:
        raise TxError(f"Satış miktarı mevcut bakiyeyi aşıyor. Mevcut: {old_qty:g}")
    realized = (price - old_cost) * qty
    con.execute("UPDATE assets SET quantity=?,updated_at=? WHERE id=?", (max(0.0, old_qty - qty), _now(), asset["id"]))
    return realized, realized * rates.get(currency, 1.0)


def _sale_pnl(asset, qty, price, currency, rates):
    """Bakiyeye dokunmadan satış kârını hesaplar."""
    realized = (price - float(asset["cost_per_unit"])) * qty
    return realized, realized * rates.get(currency, 1.0)


def _revert_tx(con, tx, asset):
    """Daha önce uygulanmış bir alış/satışın varlık bakiyesi üzerindeki etkisini geri alır."""
    if not asset or tx["transaction_type"] not in ("Alış", "Satış"):
        return
    qty, price = float(tx["quantity"]), float(tx["unit_price"])
    cur_qty, cur_cost = float(asset["quantity"]), float(asset["cost_per_unit"])
    if tx["transaction_type"] == "Alış":
        new_qty = max(0.0, cur_qty - qty)
        new_cost = max(0.0, (cur_qty * cur_cost - qty * price) / new_qty) if new_qty > 1e-12 else 0.0
        con.execute("UPDATE assets SET quantity=?,cost_per_unit=?,updated_at=? WHERE id=?",
                    (new_qty, new_cost, _now(), asset["id"]))
    else:
        con.execute("UPDATE assets SET quantity=?,updated_at=? WHERE id=?", (cur_qty + qty, _now(), asset["id"]))


@app.post("/api/transactions")
def add_transaction():
    d = request.get_json(force=True)
    try:
        p = _tx_payload(d)
        apply_flag = d.get("apply", True) not in (False, 0, "0", "false", "False")
        with db() as con:
            rates = fx_map(con)
            asset = _find_asset(con, p["asset_id"], p["name"])
            currency = p["currency"] or (asset["currency"] if asset else "TRY")
            if currency not in rates:
                raise TxError(f"'{currency}' için kur tanımlı değil.")
            realized = realized_try = 0.0
            applied = 0
            if asset and p["kind"] in ("Alış", "Satış"):
                if currency != asset["currency"]:
                    raise TxError(f"İşlem para birimi varlığın para birimiyle aynı olmalı ({asset['currency']}).")
                if apply_flag:
                    realized, realized_try = _apply_tx(con, asset, p["kind"], p["qty"], p["price"], currency, rates)
                    applied = 1
                elif p["kind"] == "Satış":
                    realized, realized_try = _sale_pnl(asset, p["qty"], p["price"], currency, rates)
            elif p["kind"] == "Temettü":
                realized = p["qty"] * p["price"]
                realized_try = realized * rates.get(currency, 1.0)
            con.execute("""INSERT INTO transactions(asset_id,asset_name,transaction_type,quantity,unit_price,currency,
                transaction_date,notes,created_at,applied,realized_pnl,realized_pnl_try)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (
                asset["id"] if asset else None, asset["name"] if asset else p["name"], p["kind"], p["qty"], p["price"],
                currency, p["date"], p["notes"], _now(), applied, realized, realized_try))
            save_snapshot(con)
        return jsonify(ok=True, realized_pnl=realized, realized_pnl_try=realized_try)
    except TxError as exc:
        return jsonify(error=str(exc)), 400


@app.route("/api/transactions/<int:tx_id>", methods=["PUT", "DELETE"])
def modify_transaction(tx_id):
    try:
        with db() as con:
            tx = con.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
            if not tx:
                return jsonify(error="İşlem bulunamadı."), 404
            rates = fx_map(con)
            orig_asset = con.execute("SELECT * FROM assets WHERE id=?", (tx["asset_id"],)).fetchone() if tx["asset_id"] else None
            reapply = bool(tx["applied"]) and orig_asset is not None

            if request.method == "DELETE":
                if reapply:
                    _revert_tx(con, tx, orig_asset)
                con.execute("DELETE FROM transactions WHERE id=?", (tx_id,))
                save_snapshot(con)
                return jsonify(ok=True)

            p = _tx_payload(request.get_json(force=True))
            if reapply:
                _revert_tx(con, tx, orig_asset)
            asset = _find_asset(con, p["asset_id"], p["name"])
            currency = p["currency"] or (asset["currency"] if asset else tx["currency"])
            if currency not in rates:
                raise TxError(f"'{currency}' için kur tanımlı değil.")
            realized = realized_try = 0.0
            applied = 0
            if p["kind"] in ("Alış", "Satış"):
                if reapply and not asset:
                    raise TxError("Bu işlem bir varlığın bakiyesine uygulanmış; varlık adı mevcut bir varlıkla eşleşmeli.")
                if asset:
                    if currency != asset["currency"]:
                        raise TxError(f"İşlem para birimi varlığın para birimiyle aynı olmalı ({asset['currency']}).")
                    if reapply:
                        realized, realized_try = _apply_tx(con, asset, p["kind"], p["qty"], p["price"], currency, rates)
                        applied = 1
                    elif p["kind"] == "Satış":
                        realized, realized_try = _sale_pnl(asset, p["qty"], p["price"], currency, rates)
            elif p["kind"] == "Temettü":
                realized = p["qty"] * p["price"]
                realized_try = realized * rates.get(currency, 1.0)
            con.execute("""UPDATE transactions SET asset_id=?,asset_name=?,transaction_type=?,quantity=?,unit_price=?,
                currency=?,transaction_date=?,notes=?,applied=?,realized_pnl=?,realized_pnl_try=? WHERE id=?""", (
                asset["id"] if asset else None, asset["name"] if asset else p["name"], p["kind"], p["qty"], p["price"],
                currency, p["date"], p["notes"], applied, realized, realized_try, tx_id))
            save_snapshot(con)
        return jsonify(ok=True)
    except TxError as exc:
        return jsonify(error=str(exc)), 400


@app.get("/api/transactions")
def transactions():
    with db() as con:
        return jsonify([dict(r) for r in con.execute("SELECT * FROM transactions ORDER BY transaction_date DESC,id DESC")])


# ----------------------------------------------------------------------------
# Kurlar
# ----------------------------------------------------------------------------
@app.route("/api/rates", methods=["GET", "POST"])
def rates_api():
    if request.method == "POST":
        d = request.get_json(force=True)
        if not isinstance(d, dict):
            return jsonify(error="Geçersiz istek."), 400
        parsed = {}
        for cur, value in d.items():
            code = str(cur).strip().upper()
            if not code:
                continue
            try:
                val = float(value)
            except (TypeError, ValueError):
                return jsonify(error=f"{code} için geçersiz kur."), 400
            if val <= 0:
                return jsonify(error="Kur sıfırdan büyük olmalı."), 400
            if code != "TRY":
                parsed[code] = val
        with db() as con:
            now = _now()
            for code, val in parsed.items():
                con.execute("INSERT INTO fx_rates(currency,try_rate,updated_at) VALUES(?,?,?) ON CONFLICT(currency) DO UPDATE SET try_rate=excluded.try_rate,updated_at=excluded.updated_at",
                            (code, val, now))
            save_snapshot(con)
            return jsonify(fx_map(con))
    with db() as con:
        return jsonify(fx_map(con))


@app.delete("/api/rates/<code>")
def delete_rate(code):
    code = code.strip().upper()
    if code in ("TRY", "USD", "EUR", "GBP", "USDT", "GAUTRY", "GAGTRY"):
        return jsonify(error="Bu kur sistem tarafından kullanıldığı için silinemez."), 400
    with db() as con:
        used = con.execute("SELECT COUNT(*) FROM assets WHERE upper(currency)=?", (code,)).fetchone()[0]
        if used:
            return jsonify(error=f"{used} varlık bu para birimini kullanıyor; önce onları değiştirin."), 400
        con.execute("DELETE FROM fx_rates WHERE currency=?", (code,))
    return jsonify(ok=True)


# ----------------------------------------------------------------------------
# Analitik / Temettü
# ----------------------------------------------------------------------------
@app.get("/api/analytics")
def analytics():
    with db() as con:
        snaps = [dict(r) for r in con.execute("SELECT * FROM snapshots ORDER BY snapshot_date")]
        txs = [dict(r) for r in con.execute("SELECT * FROM transactions ORDER BY transaction_date")]
        assets, rates, total, cost, realized = portfolio_totals(con)
    returns, drawdown = [], []
    peak = 0.0
    prev = None
    for s in snaps:
        val = float(s["total_try"])
        peak = max(peak, val)
        dd = ((val / peak) - 1) * 100 if peak else 0
        drawdown.append({"date": s["snapshot_date"], "drawdown": dd})
        daily = ((val / prev) - 1) * 100 if prev else 0
        returns.append({"date": s["snapshot_date"], "return_pct": daily, "value": val})
        prev = val
    series = [x["return_pct"] for x in returns[1:]]
    avg = sum(series) / len(series) if series else 0
    variance = sum((x - avg) ** 2 for x in series) / len(series) if series else 0
    vol = variance ** 0.5 * (252 ** 0.5)
    sharpe = (avg * 252 ** 0.5 / (variance ** 0.5)) if variance > 0 else 0
    max_dd = min((x["drawdown"] for x in drawdown), default=0)
    realized_total = sum(float(t.get("realized_pnl_try") or 0) for t in txs)
    return jsonify(history=snaps, returns=returns, drawdown=drawdown, annualized_volatility=vol,
        sharpe=sharpe, max_drawdown=max_dd, realized_pnl_try=realized_total,
        unrealized_pnl_try=total - cost, total_try=total, total_cost_try=cost)


@app.get("/api/dividends")
def dividends_api():
    with db() as con:
        rows = [dict(r) for r in con.execute("""SELECT d.*,a.name,a.symbol,a.quantity FROM dividend_events d
            JOIN assets a ON a.id=d.asset_id ORDER BY d.event_date DESC LIMIT 1000""")]
        totals = {}
        for row in rows:
            key = row["name"]
            totals[key] = totals.get(key, 0.0) + float(row["amount_per_share"] or 0) * float(row["quantity"] or 0)
    return jsonify(events=rows, estimated_by_asset=[{"name": k, "estimated": v} for k, v in totals.items()], count=len(rows))


# ----------------------------------------------------------------------------
# Piyasa verisi
# ----------------------------------------------------------------------------
def _http_json(url: str, *, params=None, timeout=12):
    response = requests.get(url, params=params, timeout=timeout, headers={
        "User-Agent": "PortfolioIntelligence/5.0 (+market-data)",
        "Accept": "application/json"
    })
    response.raise_for_status()
    return response.json()


COIN_IDS = {
    "BTC": "bitcoin", "XBT": "bitcoin", "ETH": "ethereum", "SOL": "solana",
    "BNB": "binancecoin", "XRP": "ripple", "ADA": "cardano", "DOGE": "dogecoin",
    "AVAX": "avalanche-2", "DOT": "polkadot", "LINK": "chainlink", "TRX": "tron",
    "TON": "the-open-network", "SHIB": "shiba-inu", "LTC": "litecoin",
    "BCH": "bitcoin-cash", "ATOM": "cosmos", "NEAR": "near", "APT": "aptos",
    "SUI": "sui", "USDT": "tether", "USDC": "usd-coin"
}


def _coin_id(symbol: str) -> str:
    raw = (symbol or "").strip()
    return COIN_IDS.get(raw.upper(), raw.lower())


def _coingecko_prices(ids: list[str]) -> dict[str, float]:
    """Birden çok coin için USD fiyatını tek istekte alır."""
    payload = _http_json("https://api.coingecko.com/api/v3/simple/price",
                         params={"ids": ",".join(ids), "vs_currencies": "usd"}, timeout=18)
    out = {}
    for cid in ids:
        value = (payload.get(cid) or {}).get("usd")
        if value is not None and float(value) > 0:
            out[cid] = float(value)
    return out


def _yahoo_chart(symbol: str, rng: str = "1mo", interval: str = "1d") -> dict:
    """Yahoo Finance günlük grafik verisi: güncel fiyat, önceki kapanış, kapanış serisi, para birimi."""
    symbol = (symbol or "").strip()
    if not symbol:
        raise RuntimeError("Yahoo Finance sembolü boş.")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{requests.utils.quote(symbol, safe='')}"
    payload = _http_json(url, params={"range": rng, "interval": interval}, timeout=14)
    result = (payload.get("chart", {}).get("result") or [None])[0]
    if not result:
        raise RuntimeError(f"Yahoo Finance '{symbol}' için veri döndürmedi.")
    meta = result.get("meta") or {}
    stamps = result.get("timestamp") or []
    closes = ((result.get("indicators", {}).get("quote") or [{}])[0]).get("close") or []
    offset = int(meta.get("gmtoffset") or 0)
    series = []
    for t, c in zip(stamps, closes):
        if c is None:
            continue
        day = datetime.fromtimestamp(t + offset, tz=timezone.utc).date().isoformat()
        series.append((day, float(c), t))
    price = meta.get("regularMarketPrice")
    if price is None and series:
        price = series[-1][1]
    if price is None or float(price) <= 0:
        raise RuntimeError(f"Yahoo Finance '{symbol}' için fiyat döndürmedi.")
    prev = None
    if series:
        last_t = series[-1][2]
        mtime = meta.get("regularMarketTime")
        today_bar = mtime is not None and last_t <= mtime < last_t + 86400
        if today_bar:
            prev = series[-2][1] if len(series) >= 2 else None
        else:
            prev = series[-1][1]
    if prev is None:
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    return {
        "symbol": symbol, "price": float(price), "prev": float(prev) if prev else None,
        "series": [(d, c) for d, c, _ in series], "currency": meta.get("currency"),
    }


def _yahoo_price(symbol: str) -> float:
    symbol = (symbol or "").strip().upper()
    if not symbol:
        raise RuntimeError("Yahoo Finance sembolü boş.")
    try:
        return _yahoo_chart(symbol, "5d")["price"]
    except Exception:
        pass
    try:
        import yfinance as yf
        value = yf.Ticker(symbol).fast_info.get("last_price")
        if value and float(value) > 0:
            return float(value)
    except Exception as exc:
        raise RuntimeError(f"Yahoo Finance erişilemedi: {str(exc)[:100]}")
    raise RuntimeError(f"Yahoo Finance '{symbol}' için fiyat döndürmedi.")


def _get_fx_rates() -> tuple[dict, str]:
    try:
        data = _http_json("https://open.er-api.com/v6/latest/TRY", timeout=12)
        if data.get("result") == "success" and data.get("rates"):
            rates = data["rates"]
            out = {c: 1.0 / float(rates[c]) for c in ("USD", "EUR", "GBP") if float(rates.get(c, 0) or 0) > 0}
            if "USD" in out:
                return out, "ExchangeRate-API"
    except Exception:
        pass
    out = {}
    for cur in ("USD", "EUR", "GBP"):
        try:
            out[cur] = _yahoo_price(f"{cur}TRY=X")
        except Exception:
            pass
    if out:
        return out, "Yahoo Finance FX"
    raise RuntimeError("Döviz kurları alınamadı.")


def _metal_usd_oz(code: str) -> tuple[float, str]:
    try:
        data = _http_json(f"https://api.gold-api.com/price/{code}", timeout=12)
        value = data.get("price")
        if value and float(value) > 0:
            return float(value), "Gold-API"
    except Exception:
        pass
    ticker = "GC=F" if code == "XAU" else "SI=F"
    return _yahoo_price(ticker), "Yahoo Finance"


_QUOTE_CACHE: dict = {"ts": 0.0, "data": None}
_QUOTE_TTL = 60  # saniye


def _series_lookup(series):
    dates = [d for d, _ in series]
    vals = [v for _, v in series]

    def at(day):
        if not vals:
            return None
        return vals[max(bisect_right(dates, day) - 1, 0)]
    return at


def _gram_series(metal: dict, usd: dict | None):
    if not usd or not usd["series"]:
        return []
    at = _series_lookup(usd["series"])
    return [(d, v * at(d) / OZ_GRAM) for d, v in metal["series"] if at(d)]


def _make_quote(key, label, unit, price, prev, series, source, **extra):
    valid_prev = prev if prev and prev > 0 else None
    return {
        "key": key, "label": label, "unit": unit, "price": price, "prev": valid_prev,
        "change_pct": ((price / valid_prev) - 1) * 100 if valid_prev else None,
        "change_abs": (price - valid_prev) if valid_prev else None,
        "series": [{"d": d, "v": v} for d, v in series], "source": source, **extra,
    }


def _build_quotes(force: bool = False) -> dict:
    """Altın, gümüş, USD ve EUR için fiyat + günlük değişim. 60 sn önbelleğe alınır."""
    now_ts = time.time()
    if not force and _QUOTE_CACHE["data"] and now_ts - _QUOTE_CACHE["ts"] < _QUOTE_TTL:
        return _QUOTE_CACHE["data"]

    symbols = {"usd": "USDTRY=X", "eur": "EURTRY=X", "gbp": "GBPTRY=X", "xau": "GC=F", "xag": "SI=F"}
    raw, errors = {}, {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {k: pool.submit(_yahoo_chart, s) for k, s in symbols.items()}
        for k, fut in futures.items():
            try:
                raw[k] = fut.result()
            except Exception as exc:
                errors[k] = str(exc)[:120]

    with db() as con:
        stored = fx_map(con)

    fallback_fx = {}
    if not all(k in raw for k in ("usd", "eur", "gbp")):
        try:
            fallback_fx, _ = _get_fx_rates()
        except Exception:
            fallback_fx = {}

    live = {}  # yalnızca gerçekten canlı alınan kurlar (veritabanına yazılabilir)

    def fx_quote(key, label, code):
        if key in raw:
            r = raw[key]
            live[code] = r["price"]
            return _make_quote(key, label, "₺", r["price"], r["prev"], r["series"], "Yahoo Finance")
        if code in fallback_fx:
            live[code] = fallback_fx[code]
            return _make_quote(key, label, "₺", fallback_fx[code], None, [], "ExchangeRate-API")
        return _make_quote(key, label, "₺", float(stored.get(code, 0)), None, [], "Kayıtlı kur")

    usd_q = fx_quote("usd", "USD/TRY", "USD")
    eur_q = fx_quote("eur", "EUR/TRY", "EUR")
    if "gbp" in raw:
        live["GBP"] = raw["gbp"]["price"]
    elif "GBP" in fallback_fx:
        live["GBP"] = fallback_fx["GBP"]
    usd_p = usd_q["price"] or float(stored.get("USD", 0))

    def metal_quote(key, label, rk, stored_key, code):
        if rk in raw:
            m, u = raw[rk], raw.get("usd")
            price = m["price"] * usd_p / OZ_GRAM
            prev = (m["prev"] * u["prev"] / OZ_GRAM) if (u and m["prev"] and u["prev"]) else None
            ons_change = ((m["price"] / m["prev"]) - 1) * 100 if m["prev"] else None
            live[stored_key] = price
            return _make_quote(key, label, "₺/gr", price, prev, _gram_series(m, u), "Yahoo Finance",
                               ons_usd=m["price"], ons_change_pct=ons_change)
        try:
            ounce, src = _metal_usd_oz(code)
            price = ounce * usd_p / OZ_GRAM
            live[stored_key] = price
            return _make_quote(key, label, "₺/gr", price, None, [], src, ons_usd=ounce, ons_change_pct=None)
        except Exception:
            return _make_quote(key, label, "₺/gr", float(stored.get(stored_key, 0)), None, [], "Kayıtlı kur",
                               ons_usd=None, ons_change_pct=None)

    gold_q = metal_quote("gold", "Gram altın", "xau", "GAUTRY", "XAU")
    silver_q = metal_quote("silver", "Gram gümüş", "xag", "GAGTRY", "XAG")

    data = {
        "quotes": [gold_q, silver_q, usd_q, eur_q],
        "fx": {k: float(v) for k, v in live.items() if v and float(v) > 0},
        "errors": errors,
        "updated_at": _now(),
    }
    _QUOTE_CACHE["ts"] = now_ts
    _QUOTE_CACHE["data"] = data
    return data


@app.get("/api/market/quotes")
def market_quotes():
    return jsonify(_build_quotes(force=request.args.get("force") == "1"))


def _asset_kind(a: dict) -> str:
    cat = _norm(a.get("category"))
    src = _norm(a.get("price_source"))
    if any(k in cat for k in ("doviz", "forex", "currency")):
        return "fx"
    if any(k in cat for k in ("kripto", "crypto")) or src in ("coingecko", "crypto"):
        return "crypto"
    if any(k in cat for k in ("emtia", "altin", "gumus", "metal")):
        return "metal"
    return "equity"


@app.post("/api/market/refresh")
def market_refresh():
    updated, errors = [], []
    now = _now()
    with db() as con:
        assets = [dict(r) for r in con.execute("SELECT * FROM assets WHERE lower(COALESCE(price_source,'manual'))!='manual'")]
        rates = fx_map(con)

    snap = _build_quotes(force=True)
    rates.update({k: float(v) for k, v in snap["fx"].items() if v and float(v) > 0})
    usd_try = float(rates.get("USD") or 34.5)
    rates["USDT"] = usd_try

    def symbol_of(a):
        return str(a.get("market_symbol") or a.get("symbol") or "").strip()

    # Kripto: tek istekte; hisse/fon: paralel
    coin_ids = {a["id"]: _coin_id(symbol_of(a)) for a in assets if _asset_kind(a) == "crypto"}
    coin_prices, coin_error = {}, None
    ids = sorted({i for i in coin_ids.values() if i})
    if ids:
        try:
            coin_prices = _coingecko_prices(ids)
        except Exception as exc:
            coin_error = str(exc)[:150]

    equity_results = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {a["id"]: pool.submit(_yahoo_chart, symbol_of(a), "5d") for a in assets if _asset_kind(a) == "equity"}
        for aid, fut in futures.items():
            try:
                equity_results[aid] = fut.result()
            except Exception as exc:
                equity_results[aid] = exc

    def native_price(a):
        kind = _asset_kind(a)
        currency = str(a.get("currency") or "TRY").upper()
        cur_rate = float(rates.get(currency) or 1.0)
        symbol = symbol_of(a)
        if kind == "fx":
            code = (symbol or currency).upper().replace("/TRY", "").replace("=X", "")
            if code.endswith("TRY") and code not in rates:
                code = code[:-3]
            price_try = 1.0 if code in ("", "TRY") else rates.get(code)
            if not price_try:
                raise RuntimeError(f"'{code}' için kur bulunamadı.")
        elif kind == "crypto":
            cid = coin_ids.get(a["id"])
            if not cid:
                raise RuntimeError("CoinGecko ID/sembolü girilmemiş.")
            usd_price = coin_prices.get(cid)
            if usd_price is None:
                raise RuntimeError(coin_error or f"CoinGecko '{cid}' için fiyat döndürmedi.")
            price_try = usd_price * usd_try
        elif kind == "metal":
            is_silver = any(k in _norm(symbol + a.get("name", "")) for k in ("xag", "silver", "gumus"))
            price_try = rates.get("GAGTRY" if is_silver else "GAUTRY")
            if not price_try:
                raise RuntimeError("Altın/gümüş gram fiyatı alınamadı.")
        else:
            res = equity_results.get(a["id"])
            if res is None:
                raise RuntimeError("Piyasa sembolü girilmemiş.")
            if isinstance(res, Exception):
                raise RuntimeError(str(res))
            price = res["price"]
            qcur = res.get("currency") or currency
            if qcur == "GBp":
                price, qcur = price / 100, "GBP"
            qcur = str(qcur).upper()
            if qcur == currency:
                return price
            q_rate = 1.0 if qcur == "TRY" else rates.get(qcur)
            if not q_rate:
                raise RuntimeError(f"{qcur} kuru bulunamadı.")
            price_try = price * q_rate
        return price_try / cur_rate if cur_rate > 0 else price_try

    price_updates = []
    for a in assets:
        try:
            price = native_price(a)
            if price and price > 0:
                price_updates.append((float(price), now, a["id"]))
                updated.append({"id": a["id"], "name": a["name"], "price": float(price)})
        except Exception as exc:
            errors.append({"name": str(a.get("name") or ""), "error": str(exc)[:150]})

    with db() as con:
        for cur, rate in rates.items():
            if rate and float(rate) > 0 and cur in snap["fx"] or cur == "USDT":
                con.execute("""INSERT INTO fx_rates(currency,try_rate,updated_at) VALUES(?,?,?)
                    ON CONFLICT(currency) DO UPDATE SET try_rate=excluded.try_rate,updated_at=excluded.updated_at""",
                    (cur, float(rate), now))
        if price_updates:
            con.executemany("UPDATE assets SET unit_price=?,updated_at=? WHERE id=?", price_updates)
        save_snapshot(con)

    return jsonify(ok=True, updated_count=len(updated), error_count=len(errors), errors=errors,
                   market_errors=snap.get("errors", {}))


# ----------------------------------------------------------------------------
# İçe / dışa aktarma
# ----------------------------------------------------------------------------
IMPORT_ALIASES = {
    "name": ["name", "ad", "adi", "isim", "varlik", "varlik adi"],
    "symbol": ["symbol", "sembol", "kod"],
    "category": ["category", "kategori", "tur"],
    "account": ["account", "hesap", "kurum", "araci kurum"],
    "quantity": ["quantity", "miktar", "adet"],
    "unit_price": ["unit_price", "birim fiyat", "fiyat", "guncel fiyat"],
    "currency": ["currency", "para birimi"],
    "cost_per_unit": ["cost_per_unit", "maliyet", "birim maliyet", "ortalama maliyet"],
    "notes": ["notes", "not", "notlar", "aciklama"],
    "price_source": ["price_source", "fiyat kaynagi"],
    "market_symbol": ["market_symbol", "piyasa sembolu"],
}


def _num(value, default=0.0):
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        f = float(value)
        return f if f == f else default
    s = str(value).strip().replace(" ", "")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return default


@app.post("/api/import")
def import_excel():
    if "file" not in request.files:
        return jsonify(error="Excel dosyası seçilmedi."), 400
    f = request.files["file"]
    try:
        book = pd.ExcelFile(f)
        frame = pd.read_excel(book, book.sheet_names[0]).fillna("")
        header_map = {}
        for col in frame.columns:
            key = _norm(col)
            for field, aliases in IMPORT_ALIASES.items():
                if key in aliases and field not in header_map.values():
                    header_map[col] = field
                    break
        by_field = {v: k for k, v in header_map.items()}
        now = _now()
        count = skipped = 0
        with db() as con:
            known = fx_map(con)
            seen = {(_norm(r["name"]), _norm(r["account"])) for r in con.execute("SELECT name,account FROM assets")}
            for _, row in frame.iterrows():
                def get(field, fallback=""):
                    col = by_field.get(field)
                    return row[col] if col is not None else fallback
                name = str(get("name", row.iloc[0] if "name" not in by_field else "")).strip()
                if not name:
                    continue
                account = str(get("account")).strip()
                ident = (_norm(name), _norm(account))
                if ident in seen:
                    skipped += 1
                    continue
                seen.add(ident)
                currency = (str(get("currency")).strip().upper() or "TRY")
                if currency not in known:
                    currency = "TRY"
                con.execute("""INSERT INTO assets(name,symbol,category,account,quantity,unit_price,currency,cost_per_unit,
                    notes,created_at,updated_at,price_source,market_symbol) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    name, str(get("symbol")).strip(), str(get("category")).strip() or "Diğer", account,
                    max(0.0, _num(get("quantity"))), max(0.0, _num(get("unit_price"))), currency,
                    max(0.0, _num(get("cost_per_unit"))), str(get("notes")), now, now,
                    str(get("price_source")).strip() or "manual", str(get("market_symbol")).strip()))
                count += 1
            save_snapshot(con)
        return jsonify(ok=True, imported=count, skipped=skipped)
    except Exception as exc:
        return jsonify(error=f"Excel okunamadı: {exc}"), 400


@app.get("/api/export")
def export_excel():
    with db() as con:
        assets = pd.read_sql_query("SELECT * FROM assets", con)
        tx = pd.read_sql_query("SELECT * FROM transactions", con)
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        assets.to_excel(writer, sheet_name="Varlıklar", index=False)
        tx.to_excel(writer, sheet_name="İşlemler", index=False)
    out.seek(0)
    return send_file(out, as_attachment=True, download_name="portfoy_yedek.xlsx", mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


init_db()
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)

from flask import Flask

app = Flask(__name__)

@app.route('/')
def home():
    return "Merhaba, Vercel!"

if __name__ == '__main__':
    app.run
