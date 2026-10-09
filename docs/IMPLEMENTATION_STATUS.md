# CursorHive: фактическое состояние

Дата: 9 октября 2026 года. Версия: 0.1.0, локальный прототип.

## Реализовано

- CLI doctor/start/demo/status/wait/update/stop/recover/mcp.
- Отдельный supervisor запуска и авторизованный loopback RPC.
- SQLite WAL: атомарный claim, один активный claim на worker, версии владения, журнал событий, идемпотентные операции.
- Раздельные controller/worker инструменты MCP, credentials по ходам и revision binding. Старый ход не подтверждает новую revision.
- Два тестовых процесса: A публикует файл, B импортирует его и использует в собственном результате.
- Сохранённый dirty baseline, private Git рабочих копий, неизменяемые публикации, проверка конфликтов импорта.
- Candidate, integration claim, закрытие update admission, stop и сохранённое восстановление.
- Наблюдаемый argv check над отдельным snapshot, проверка изменения inputs и отмена во время check.
- Export-only: bytes, bases и deletion manifest; исходный проект и index не изменяются.
- Headless Cursor adapter: argv без shell, streaming, session_id, resume, MCP enable, явный force.
- Пакет Cursor Plugin, сборщик и установщик с сохранением прежней версии и отказом перезаписывать чужой каталог.

## Граница подтверждения

9 октября 2026 года прошли все 31 тест на Linux/Python 3.12, `ruff check .` и `ruff format --check .`. Тесты проверяют реальный SQLite, HTTP, subprocess и stdio MCP SDK, включая запуск установленного пакета плагина без исходного checkout. Cursor adapter проверяется отдельным явно обозначенным mock CLI. Это **не квалификация настоящего Cursor**.

В среде разработки Cursor CLI отсутствует. Вход реального адаптера по умолчанию блокируется; флаг `--experimental-cursor` является явным включением прототипа и не меняет `live_qualified=false`. `--force` отдельно включается пользователем и автоматически не добавляется.

Doctor пока делает только локальный inventory без модели. Предложенный в архитектурном плане `doctor --live` ещё не реализован: нужны запись/deny, реальные MCP identities, resume, native approvals и отмена на конкретной версии CLI. Не следует подменять эту проверку успешным mock-тестом.

## Что пока отсутствует

- Native fork/полный перенос текущего IDE чата; native prompt hooks.
- Проверенная установка и отображение плагина в Cursor IDE.
- Live qualification и матрица Windows/macOS/Linux Cursor.
- Windows Job Objects, полное наблюдение detached descendants и доказанная OS ACL-политика для credentials.
- Автоматическое применение в оригинальном проекте, durable apply journal и TOCTOU backend.
- Binary, executable mode, symlink, submodule и LFS; автоматический merge конфликтов.
- Настройка проектных зависимостей, preview/service ownership, ранние component receipts и MCP hive_check_request.
- VSIX, ACP, внешние интеграции, ERGO MS, стоимость/usage, несколько моделей и четыре worker.

Все workspaces сохраняются. Частично записанный export имеет incomplete marker и не считается готовым. Если системный инспектор не даёт creation time, runtime использует native Popen ownership для своего child; сохранённый неизвестный PID не даёт права завершить процесс при recovery. Наблюдение за произвольно отделившимися потомками остаётся ограничением.

## Соответствие этапам плана

| Этап | Состояние |
| --- | --- |
| 0, qualification | Inventory готов; live probes впереди |
| 1, board | Реализован и проверен конкурентными/идемпотентными сценариями |
| 2, workspace/export | Реализован для небольших UTF-8 Git-проектов |
| 3, Cursor workers | Адаптер написан; проверен mock CLI, настоящие сессии не запускались |
| 4, verification | Final snapshot executor готов; ранние component receipts впереди |
| 5, plugin MVP | Сборка/установка/MCP smoke готовы; IDE/live проверка впереди |
| 6–7 | Будущая разработка |

Ближайшая работа: запустить doctor на целевой Windows, квалифицировать CLI и MCP на небольшой тестовой репе, затем включить реальный двухагентный сценарий. Самостоятельный продукт не требует ERGO MS.
