# CursorHive

Самостоятельный локальный runtime и плагин Cursor для двух агентов с общей очередью, публикациями и независимыми рабочими копиями.

Первый выпуск использует экспорт результата. Исходный проект и Git index не изменяются.

Версия **0.1.0 — прототип**. Локальная очередь, два тестовых процесса, MCP и экспорт работают. Адаптер настоящего Cursor написан, но live qualification ещё не выполнена: в среде разработки нет Cursor CLI. [Фактический статус](docs/IMPLEMENTATION_STATUS.md) отделён от [архитектурного плана](CURSOR_HIVE_STRUCTURE.md).

## Установка для разработки

Требуются Python 3.12 и Git. Для реальных агентов также нужны Cursor CLI и его существующая авторизация.

```bash
python -m venv .venv
# Активируйте .venv средствами своей ОС.
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
cursor-hive doctor
```

На Windows активация: `.venv\Scripts\Activate.ps1`. На Linux/macOS: `source .venv/bin/activate`. Doctor не вызывает модель и не расходует квоту.

## Проверяемый цикл без LLM

На небольшой тестовой Git-репе:

```bash
cursor-hive demo /path/to/test-project --wait
```

Два отдельных fake-agent процесса создают `hive-demo-a.txt` и `hive-demo-b.txt` в своих копиях. B читает и применяет публикацию A. Итог возвращается как export в каталоге состояния. **Demo проверяет координацию, а не выполняет пользовательскую задачу программирования.**

CLI печатает run_id. Через него доступны:

```bash
cursor-hive status RUN_UUID
cursor-hive wait RUN_UUID --timeout 60
cursor-hive update RUN_UUID "Полный обновлённый запрос" --expected-revision 1
cursor-hive stop RUN_UUID
cursor-hive recover RUN_UUID /new/export-directory
```

`--state-dir` указывается перед подкомандой, например `cursor-hive --state-dir /private/state demo /project --wait`. Состояние должно находиться вне исходного проекта. Закрытие контроллера не останавливает supervisor.

Профиль проверки — явный argv, без shell:

```bash
cursor-hive demo /path/to/test-project --wait --check-argv '["python", "-m", "unittest", "discover"]'
```

Этот профиль должен реально проверять captured inputs в подходящем окружении. Receipt подтверждает наблюдённый исход команды и записанные hashes, а не полноту покрытия или переносимость окружения. `exported` не означает, что файлы применены к оригиналу или готовы там к запуску.

## Экспериментальный Cursor adapter

Обычный start требует квалификации и сейчас возвращает понятный отказ. Для явного испытания прототипа на тестовой репе:

```bash
cursor-hive start /path/to/test-project "Реализуй задачу ..." --experimental-cursor --model MODEL_ID
```

Если выбран автоматический профиль, `--force` добавляется отдельно. Плагин не включает его скрыто. Native permissions остаются у Cursor; полная копия IDE-чата, глобальных инструментов и live overrides не обещается. При отсутствующих MCP, streaming, resume или необходимых правах результатом будет ошибка/сохранённая работа, а не выдуманный успех.

## Плагин Cursor

```bash
python scripts/build_plugin.py
python scripts/install_plugin.py dist/cursor-hive
```

Установщик создаёт отдельное Python-окружение, устанавливает закреплённые зависимости и копирует самодостаточные ресурсы в `~/.cursor/plugins/local/cursor-hive`. Старая собственная версия сохраняется отдельно; чужой каталог не перезаписывается. Для уже подготовленного окружения можно передать `--runtime-python /path/to/python`.

После установки: Developer: Reload Window, затем проверить компоненты в Customize. Команды: hive-run, hive-status, hive-update, hive-stop, hive-recover; отображение namespace зависит от Cursor. Сборка и stdio launcher проверяются тестами, реальная загрузка IDE пока не проверена.

## Разработка и проверки

```bash
python -m pip install 'ruff>=0.9,<1' 'build>=1,<2'
python -m unittest discover -s tests -v
ruff check .
ruff format --check .
python -m build --wheel
```

Тесты проверяют конкурентные claims, retry/revision, файловые конфликты, реальные subprocess/HTTP/MCP, отмену проверки, export/recovery, установленный launcher и отдельно обозначенный mock Cursor CLI. Они не расходуют модельную квоту. GitHub Actions настроен для Linux и Windows; прохождение локальных Linux-тестов не выдаётся за Windows qualification.

## Ограничения

Поддерживаются небольшие UTF-8 Git-проекты без submodule/LFS, executable modes и symlink. Зарезервированные `.cursor/mcp.json` и `.cursor/cli.json` генерируются только в worker-копиях и исключаются из дельты. Известные секретные файлы/среды исключаются; это не универсальный сканер секретов.

Workspaces остаются для восстановления. Наблюдение произвольно отделившихся процессов и Windows ACL/Job Objects ещё не квалифицированы. Автоматической доставки, ACP, VSIX и обязательной зависимости от ERGO MS нет.

Документы: [протокол](docs/protocol.md), [восстановление](docs/recovery.md), [совместимость Cursor](docs/cursor-compatibility.md). Подход основан на DeLM; этот проект не является официальным плагином авторов и не обещает их benchmark-ускорение.
