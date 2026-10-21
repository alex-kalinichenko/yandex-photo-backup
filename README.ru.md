# Бэкап Яндекс Фото (Unlimited)

Локальный возобновляемый бэкап **Яндекс Диск Unlimited Photos** (Photoslice / «Фото»).
Скачивает оригиналы на диск со состоянием в SQLite, проверкой размера и адаптивной
параллельностью, когда CDN-кластеры Яндекса тормозят или «залипают».

> Это **не** полноценный клиент дерева файлов Диска. Инструмент работает с API
> ленты фото (фото и видео в Unlimited Photos), а не с произвольными папками Диска.

[English version](README.md)

## Возможности

- Авторизация по cookies (без регистрации OAuth-приложения)
- Конвейер: сбор манифеста → скачивание → проверка
- SQLite WAL — можно безопасно прерывать и продолжать
- Адаптивный пул воркеров (детект stall, отказ от мёртвых CDN-хостов)
- Фильтр по типу (`photos` / `videos` / `all`)
- Порядок по годам (сначала недавние годы, текущий год — в конце)
- Опционально только объекты с MD5 (`--with-md5-only`)

## Требования

- Python 3.11+
- Windows, macOS или Linux
- `curl` в `PATH` (в Windows 10+ есть `curl.exe`)
- Сессия Яндекса, экспортированная в cookies

```bash
pip install -r requirements.txt
```

## Авторизация

**Никогда не коммитьте `cookies.json`.** Это ваша сессия — по сути пароль.

### Вариант A — Cookie-Editor (рекомендуется)

1. Откройте [disk.yandex.ru/client/photo](https://disk.yandex.ru/client/photo) и войдите.
2. Экспортируйте cookies в JSON через [Cookie-Editor](https://cookie-editor.com/).
3. Сохраните как `cookies.json` в корне репозитория (см. `cookies.json.example`).

Нужны как минимум `Session_id` (обычно ещё и `sessionid2`).

### Вариант B — Chrome DevTools (локально)

```bash
python export_cookies_cdp.py
```

Инструкция по запуску Chrome с remote debugging — в комментариях в начале `export_cookies_cdp.py`.

### Вариант C — профиль Firefox

Если Firefox установлен и вы залогинены в Яндекс, cookies могут подхватиться автоматически.

## Быстрый старт

```bash
# Куда класть файлы и SQLite (по умолчанию: ~/YandexPhoto)
# Windows PowerShell:
$env:YANDEX_PHOTO_ROOT = 'D:\YandexPhoto'

# 1) Собрать / обновить манифест
python backup.py collect

# 2) Сначала фото (адаптивные воркеры)
python backup.py download --media photos --with-md5-only --workers 6

# 3) Затем видео
python backup.py download --media videos --workers 4

# 4) Перепроверить локальные файлы по БД
python backup.py reconcile

# 5) Сводка прогресса
python backup.py status
```

Ярлык для Windows:

```bat
yandex-photo-backup.bat download --media photos --workers 6
```

## Полезные опции

| Опция | Смысл |
|--------|--------|
| `--root PATH` | Корень бэкапа (файлы + БД + логи) |
| `--media photos\|videos\|all` | Что скачивать |
| `--workers N` | Макс. параллельных загрузок (адаптив может снизить) |
| `--with-md5-only` | Только объекты, у которых уже есть MD5 в манифесте |
| `--limit N` | Остановиться после N успешных загрузок |
| `--no-adaptive` | Фиксированное число воркеров без stall/abandon |
| `--resume-clusters` | При collect — продолжить без полного refresh кластеров |

## Вспомогательные скрипты

Задайте `YANDEX_PHOTO_ROOT` на тот же путь, что и для бэкапа.

```bash
python scripts/photo_progress.py     # прогресс фото / видео
python scripts/year_stats.py         # статистика по годам
python scripts/watchdog_download.py  # перезапуск download, если процесс упал
```

## Структура каталога бэкапа

```
YandexPhoto/
  yandex_photo_backup.sqlite   # манифест и состояние загрузок
  files/                       # скачанные оригиналы
  tmp/                         # незавершённые .part
  logs/                        # дневные логи
```

## Безопасность

- Держите `cookies.json` в секрете; смените сессию Яндекса, если файл когда-либо утёк.
- Инструмент **только читает** облако (список + скачивание).
- Не публикуйте дампы API — в них могут быть ваш `uid` и подписанные URL.

## Отказ от ответственности

Неофициальный инструмент. API и CDN Яндекса могут измениться. Используйте на свой
риск и соблюдайте правила сервиса. Не завышайте `--workers` без необходимости.

## Лицензия

MIT — см. [LICENSE](LICENSE).
