import ctypes
import configparser
from types import SimpleNamespace

from i3expo import main


def workspace(name, num, identifier, width=1920, height=1080, leaves=None):
    leaves = [] if leaves is None else leaves
    return SimpleNamespace(
        name=name,
        num=num,
        id=identifier,
        ipc_data={'output': 'eDP-1'},
        rect=SimpleNamespace(x=0, y=0, width=width, height=height),
        leaves=lambda: leaves,
    )


def reset_knowledge():
    main.GLOBAL_KNOWLEDGE = {
        'active': None,
        'prev_f_w': None,
        'wss': {},
    }


def test_named_workspaces_do_not_collide_at_num_minus_one():
    reset_knowledge()
    a = workspace('a', -1, 10)
    k = workspace('k', -1, 20)

    main.update_workspace(a, k)
    main.update_workspace(k, k)

    assert list(main.GLOBAL_KNOWLEDGE['wss']) == ['a', 'k']
    assert main.GLOBAL_KNOWLEDGE['active'] == 'k'


def test_hydration_preserves_valid_preview_with_stale_dimensions():
    reset_knowledge()
    ws = workspace('a', -1, 10, width=16, height=9)
    screenshot = [8, 6, bytearray(8 * 6 * 3)]
    main.GLOBAL_KNOWLEDGE['wss']['a'] = {
        'name': 'a',
        'screenshot': screenshot,
        'last-update': 123.0,
        'state': 0,
        'w': 8,
        'h': 6,
    }

    item = main.update_workspace(ws, ws, hydration=True)

    assert item['screenshot'] is screenshot
    assert item['last-update'] == 123.0
    assert (item['w'], item['h']) == (16, 9)


def test_hydration_discards_corrupt_preview():
    reset_knowledge()
    ws = workspace('a', -1, 10, width=16, height=9)
    main.GLOBAL_KNOWLEDGE['wss']['a'] = {
        'name': 'a',
        'screenshot': [8, 6, bytearray(1)],
        'last-update': 123.0,
        'state': 0,
        'w': 8,
        'h': 6,
    }

    item = main.update_workspace(ws, ws, hydration=True)

    assert item['screenshot'] == []
    assert item['last-update'] == 0.0


def test_live_ctypes_screenshot_buffer_is_valid():
    pixels = (ctypes.c_ubyte * 8 * 6 * 3)()

    assert main.screenshot_is_valid([8, 6, pixels])


def test_force_refresh_bypasses_rate_limit():
    ws = workspace('1', 1, 10)
    item = {'last-update': 100.0, 'state': 0}

    assert main.should_update_ws(10.0, ws, item, 101.0, force=True)


def test_missing_preview_sweep_captures_only_nonempty_workspace_and_restores_focus(
    monkeypatch,
):
    reset_knowledge()
    a = workspace('a', -1, 10, width=16, height=9, leaves=[object()])
    k = workspace('k', -1, 20, width=16, height=9, leaves=[object()])
    p = workspace('p', -1, 30, width=16, height=9)
    b = workspace('b', -1, 40, width=16, height=9, leaves=[object()])
    workspaces = [a, k, p, b]

    for ws in workspaces:
        main.update_workspace(ws, a)
    main.GLOBAL_KNOWLEDGE['wss']['b']['screenshot'] = [
        16,
        9,
        bytearray(16 * 9 * 3),
    ]

    class Connection:
        def __init__(self):
            self.current = a
            self.commands = []

        def get_tree(self):
            focused = SimpleNamespace(
                id=10 if self.current is a else self.current.id,
                workspace=lambda: self.current,
            )
            return SimpleNamespace(
                find_focused=lambda: focused,
                workspaces=lambda: workspaces,
            )

        def command(self, command):
            self.commands.append(command)
            if command.startswith('workspace'):
                for ws in workspaces:
                    if command.endswith(main.quote_i3_string(ws.name)):
                        self.current = ws
                        break
            elif command == '[con_id=10] focus':
                self.current = a

    connection = Connection()
    main.UPDATER_DEBOUNCED = SimpleNamespace(reset=lambda: None)
    main.WS_UPDATE_DEBOUNCED = SimpleNamespace(reset=lambda: None)
    monkeypatch.setattr(main.time, 'sleep', lambda _seconds: None)
    monkeypatch.setattr(main, 'update_tree_state', lambda _ws, _item: True)
    monkeypatch.setattr(
        main,
        'grab_screen',
        lambda item: [item['w'], item['h'], bytearray(item['w'] * item['h'] * 3)],
    )

    main.capture_missing_workspace_previews(connection, ['a', 'k', 'p', 'b'])

    assert connection.commands == [
        'workspace --no-auto-back-and-forth "k"',
        '[con_id=10] focus',
    ]
    assert main.GLOBAL_KNOWLEDGE['wss']['k']['screenshot']
    assert main.GLOBAL_KNOWLEDGE['wss']['p']['screenshot'] == []
    assert main.GLOBAL_KNOWLEDGE['wss']['b']['screenshot']
    assert main.GLOBAL_KNOWLEDGE['active'] == 'a'
    assert main.PREVIEW_SWEEP_RUNNING is False

    connection.commands.clear()
    main.capture_missing_workspace_previews(
        connection,
        ['a', 'k', 'p', 'b'],
        force=True,
    )
    assert connection.commands == [
        'workspace --no-auto-back-and-forth "k"',
        'workspace --no-auto-back-and-forth "b"',
        '[con_id=10] focus',
    ]


def test_i3_workspace_name_is_quoted():
    assert main.quote_i3_string('a "quoted" \\ name') == '"a \\"quoted\\" \\\\ name"'


def test_direct_key_selects_matching_workspace():
    reset_knowledge()
    main.GLOBAL_KNOWLEDGE['wss'] = {
        '1': {'name': '1'},
        'a': {'name': 'a'},
        'k': {'name': 'k'},
    }
    tiles = {0: {'ws': '1'}, 1: {'ws': 'a'}, 2: {'ws': 'k'}}

    assert main.direct_workspace_command('1', tiles) == (
        'workspace --no-auto-back-and-forth "1"'
    )
    assert main.direct_workspace_command('a', tiles) == (
        'workspace --no-auto-back-and-forth "a"'
    )
    assert main.direct_workspace_command('k', tiles) == (
        'workspace --no-auto-back-and-forth "k"'
    )
    assert main.direct_workspace_command('x', tiles) is None


def test_global_shortcut_parser():
    assert main.parse_global_shortcut('Mod4+e') == (64, 'e')
    assert main.parse_global_shortcut('Ctrl+Shift+space') == (5, 'space')
    assert main.parse_global_shortcut('') is None


def test_first_run_creates_editable_default_config(tmp_path, monkeypatch):
    config_path = tmp_path / 'i3expo' / 'config'
    config = configparser.ConfigParser(converters={'color': main.get_color})
    monkeypatch.setattr(main, 'CONFIG', config, raising=False)
    monkeypatch.setattr(main, 'CONFIG_FILE', str(config_path), raising=False)

    main.read_config()

    text = config_path.read_text(encoding='utf-8')
    assert 'bgcolor = #0A001F' in text
    assert 'frame_active_color = #00D7FF' in text
    assert 'names_color = #FF5FFF' in text
    assert 'names_font = default' in text
    assert 'startup_scan = true' in text
    assert 'toggle_shortcut = Mod4+e' in text
    assert config.get('CONF', 'bgcolor') == '#0A001F'


def test_existing_config_is_not_overwritten(tmp_path, monkeypatch):
    config_path = tmp_path / 'i3expo' / 'config'
    config_path.parent.mkdir()
    custom = '[CONF]\nbgcolor = #123456\ntoggle_shortcut =\n'
    config_path.write_text(custom, encoding='utf-8')
    config = configparser.ConfigParser(converters={'color': main.get_color})
    monkeypatch.setattr(main, 'CONFIG', config, raising=False)
    monkeypatch.setattr(main, 'CONFIG_FILE', str(config_path), raising=False)

    main.read_config()

    assert config_path.read_text(encoding='utf-8') == custom
    assert config.get('CONF', 'bgcolor') == '#123456'


def test_rename_preserves_workspace_order_and_active_key():
    reset_knowledge()
    main.GLOBAL_KNOWLEDGE['wss'] = {
        '1': {'name': '1'},
        'a': {'name': 'a'},
        'k': {'name': 'k'},
    }
    main.GLOBAL_KNOWLEDGE['active'] = 'a'
    event = SimpleNamespace(
        change='rename',
        old=SimpleNamespace(name='a'),
        current=SimpleNamespace(name='app', num=-1),
    )

    main.on_ws_rename(None, event)

    assert list(main.GLOBAL_KNOWLEDGE['wss']) == ['1', 'app', 'k']
    assert main.GLOBAL_KNOWLEDGE['active'] == 'app'


def test_empty_workspace_is_kept_but_stale_preview_is_removed():
    reset_knowledge()
    main.GLOBAL_KNOWLEDGE['wss'] = {
        'a': {'screenshot': [1], 'last-update': 10.0, 'state': 1},
        'k': {'screenshot': [2], 'last-update': 20.0, 'state': 2},
    }
    tree = SimpleNamespace(workspaces=lambda: [SimpleNamespace(name='a')])
    connection = SimpleNamespace(get_tree=lambda: tree)
    event = SimpleNamespace(change='empty')

    main.on_ws_empty(connection, event)

    assert list(main.GLOBAL_KNOWLEDGE['wss']) == ['a', 'k']
    assert main.GLOBAL_KNOWLEDGE['wss']['k']['screenshot'] == []
    assert main.GLOBAL_KNOWLEDGE['wss']['k']['last-update'] == 0.0
    assert main.GLOBAL_KNOWLEDGE['wss']['k']['state'] == 0
