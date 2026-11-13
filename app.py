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
                asset["id"] if asset else None, asset["name"] if asset else 
