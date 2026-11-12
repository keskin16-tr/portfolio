# Portfolio Intelligence 4.1

## Kurulum

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Tarayıcı: `http://127.0.0.1:5000`

## Otomatik piyasa verileri

- **Döviz:** ExchangeRate-API → Frankfurter → Yahoo Finance yedek sırası. USD, EUR ve GBP TRY kurları otomatik yenilenir. Döviz varlığında para birimi USD/EUR/GBP seçiliyse birim fiyat 1 olarak tutulur; TL değerlemesi güncel kur tablosundan yapılır.
- **Kripto:** CoinGecko → Binance (CCXT) yedek sırası. CoinGecko ID veya bilinen sembol girilebilir: `bitcoin`, `ethereum`, `BTC`, `ETH`, `SOL`, `XRP`, `BNB` vb. Para birimi TRY/USD/USDT/EUR/GBP seçilebilir.
- **Altın/gümüş:** Gold-API → Yahoo Finance vadeli kontratları yedek sırası. Gram altın/gümüş TL fiyatı ons fiyatından hesaplanır. Varlık kategorisi `Altın / Emtia` olmalı; adında veya sembolünde Altın/Gold/XAU ya da Gümüş/Silver/XAG bulunması yeterlidir.
- **Hisse/diğer emtia:** Yahoo Finance chart servisi, gerekirse yfinance kütüphanesi. BIST örnekleri `THYAO.IS`, `ASELS.IS`; ABD örnekleri `AAPL`, `MSFT`; diğer emtialar için Yahoo sembolü girin.

Varlığın **Fiyat sağlayıcı** alanı `Otomatik` olmalı. `Manuel` seçimi bilerek otomatik yenileme dışında bırakılır. Mevcut sembollü ve döviz/emtia/kripto kategorisindeki manuel kayıtlar ilk açılışta otomatiğe geçirilir.

Panel açılışında güncelleme yapılır; panel açık kaldığı sürece 15 dakikada bir tekrarlanır. Güncelleme satırında her sağlayıcının durumu ve alınan hatalar görünür. Sağlayıcıların kota, ağ erişimi ve gecikme kısıtları olabilir. Uygulama internete erişebilen bir ortamda çalışmalıdır.

## Temettü

Yahoo Finance hisse temettü geçmişini takvime alır. Geçmişteki gerçek hak kazanma miktarı bilinmediğinden otomatik olarak nakit bakiyeye yazılmaz; ekranda tahmini gösterilir.

## 4.1.1 veritabanı kilitlenmesi düzeltmesi

- SQLite bağlantıları her isteğin sonunda kesin olarak kapatılır.
- WAL modu ve 45 saniyelik `busy_timeout` etkinleştirilmiştir.
- Piyasa sağlayıcılarına yapılan uzun ağ istekleri sırasında SQLite yazma işlemi açık tutulmaz; fiyatlar önce alınır, sonra kısa bir işlemle kaydedilir.
- Güncelleme yapmadan önce uygulamayı tamamen durdurun ve `portfolio.db` dosyasının yedeğini alın. Veritabanını silmeyin.
- Aynı `portfolio.db` dosyasını aynı anda kullanan birden fazla eski uygulama süreci çalıştırmayın.
