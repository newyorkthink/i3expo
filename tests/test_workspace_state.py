import ctypes
import configparser
import pickle
from types import SimpleNamespace

from PIL import Image

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


def test_previous_release_preview_cache_is_invalidated(tmp_path, monkeypatch):
    state_path = tmp_path / 'i3expo.state'
    state = {
        'timestamp': main._unix_time_now(),
        'gknowledge': {
            'active': 'a',
            'prev_f_w': None,
            'wss': {
                'a': {
                    'name': 'a',
                    'screenshot': [2, 1, bytearray(6)],
                    'last-update': 10.0,
                    'state': 99,
                },
            },
        },
    }
    state_path.write_bytes(pickle.dumps(state))
    config = configparser.ConfigParser()
    config.read_dict({'CONF': {
        'state_f': str(state_path),
        'max_persisted_state_age_sec': '604800',
    }})
    monkeypatch.setattr(main, 'CONFIG', config, raising=False)

    loaded = main.load_global_knowledge()

    assert loaded['wss']['a']['screenshot'] == []
    assert loaded['wss']['a']['last-update'] == 0.0
    assert loaded['wss']['a']['state'] == 0


def test_live_ctypes_screenshot_buffer_is_valid():
    pixels = (ctypes.c_ubyte * 8 * 6 * 3)()

    assert main.screenshot_is_valid([8, 6, pixels])


def test_decode_ximage_converts_little_endian_bgrx():
    display_info = SimpleNamespace(
        image_byte_order=0,
        pixmap_formats=[SimpleNamespace(
            depth=24,
            bits_per_pixel=32,
            scanline_pad=32,
        )],
    )
    image = main.decode_ximage(
        bytes((3, 2, 1, 0, 30, 20, 10, 0)),
        2,
        1,
        24,
        display_info,
    )

    assert image.getpixel((0, 0)) == (1, 2, 3)
    assert image.getpixel((1, 0)) == (10, 20, 30)


def test_workspace_preview_composes_fresh_client_over_cached_frame(monkeypatch):
    config = configparser.ConfigParser()
    config.read_dict({'CONF': {'bgcolor': '#0A001F'}})
    monkeypatch.setattr(main, 'CONFIG', config, raising=False)
    leaf = SimpleNamespace(
        window=100,
        window_class='browser',
        rect=SimpleNamespace(x=1, y=1, width=2, height=1),
        window_rect=SimpleNamespace(x=0, y=0, width=2, height=1),
    )
    ws = workspace('b', -1, 20, width=4, height=3, leaves=[leaf])
    previous = [4, 3, bytearray(4 * 3 * 3)]
    red_client = Image.new('RGB', (2, 1), '#ff0000')

    screenshot, captured = main.compose_workspace_preview(
        ws,
        previous,
        capture_window=lambda _window: red_client,
    )

    result = Image.frombytes('RGB', (4, 3), bytes(screenshot[2]))
    assert captured == 1
    assert result.getpixel((1, 1)) == (255, 0, 0)
    assert result.getpixel((2, 1)) == (255, 0, 0)
    assert result.getpixel((0, 0)) == (0, 0, 0)


def test_workspace_preview_excludes_overview_and_sticky_windows(monkeypatch):
    config = configparser.ConfigParser()
    config.read_dict({'CONF': {'bgcolor': '#0A001F'}})
    monkeypatch.setattr(main, 'CONFIG', config, raising=False)
    rect = SimpleNamespace(x=0, y=0, width=2, height=1)
    leaves = [
        SimpleNamespace(
            window=998,
            window_class='unexpected-class',
            sticky=False,
            rect=rect,
            window_rect=rect,
        ),
        SimpleNamespace(
            window=200,
            window_class='clipboard-popup',
            sticky=False,
            parent=SimpleNamespace(sticky=True, parent=None),
            rect=rect,
            window_rect=rect,
        ),
        SimpleNamespace(
            window=300,
            window_class='I3EXPO',
            sticky=False,
            rect=rect,
            window_rect=rect,
        ),
        SimpleNamespace(
            window=100,
            window_class='browser',
            sticky=False,
            rect=rect,
            window_rect=rect,
        ),
    ]
    ws = workspace('b', -1, 20, width=2, height=1, leaves=leaves)
    captured_ids = []

    screenshot, captured = main.compose_workspace_preview(
        ws,
        [],
        capture_window=lambda window_id: (
            captured_ids.append(window_id) or Image.new('RGB', (2, 1), 'red')
        ),
        excluded_window_ids={998},
    )

    assert captured == 1
    assert captured_ids == [100]
    assert screenshot[0:2] == [2, 1]


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


def test_only_arrow_keys_navigate_overview():
    assert main.arrow_navigation_delta(main.pygame.K_UP) == (0, -1)
    assert main.arrow_navigation_delta(main.pygame.K_DOWN) == (0, 1)
    assert main.arrow_navigation_delta(main.pygame.K_LEFT) == (-1, 0)
    assert main.arrow_navigation_delta(main.pygame.K_RIGHT) == (1, 0)
    assert main.arrow_navigation_delta(main.pygame.K_h) is None
    assert main.arrow_navigation_delta(main.pygame.K_j) is None
    assert main.arrow_navigation_delta(main.pygame.K_k) is None
    assert main.arrow_navigation_delta(main.pygame.K_l) is None


def test_auto_capture_discovers_new_workspace_from_live_tree(monkeypatch):
    reset_knowledge()
    a = workspace('a', -1, 10, leaves=[object()])
    i = workspace('i', -1, 20, leaves=[object()])
    focused = SimpleNamespace(workspace=lambda: a)
    tree = SimpleNamespace(
        find_focused=lambda: focused,
        workspaces=lambda: [a, i],
    )
    connection = SimpleNamespace(get_tree=lambda: tree)
    config = configparser.ConfigParser()
    config.read_dict({'CONF': {
        'auto_scan_new_workspaces': 'true',
        'workspace_capture_delay_sec': '0.2',
    }})
    calls = []

    monkeypatch.setattr(main, 'CONFIG', config, raising=False)
    monkeypatch.setattr(main, 'GLOBAL_UPDATES_RUNNING', True)
    monkeypatch.setattr(main, 'PREVIEW_SWEEP_RUNNING', False)
    monkeypatch.setattr(main, 'OUTPUT_BLACKLIST', [], raising=False)
    monkeypatch.setattr(
        main,
        'capture_missing_workspace_previews',
        lambda conn, keys, delay: calls.append((conn, keys, delay)),
    )

    main.auto_capture_missing_previews(connection)

    assert list(main.GLOBAL_KNOWLEDGE['wss']) == ['a', 'i']
    assert calls == [(connection, ['a', 'i'], 0.2)]


def test_live_refresh_maps_workspaces_behind_overview_and_restores(monkeypatch):
    reset_knowledge()
    leaf = SimpleNamespace(
        id=100,
        name='browser',
        focused=False,
        window=100,
        window_class='browser',
        rect=SimpleNamespace(x=0, y=0, width=16, height=9),
        window_rect=SimpleNamespace(x=0, y=0, width=16, height=9),
    )
    a = workspace('a', -1, 10, width=16, height=9, leaves=[leaf])
    b = workspace('b', -1, 20, width=16, height=9, leaves=[leaf])
    workspaces = [a, b]
    for ws in workspaces:
        main.update_workspace(ws, a)

    class Connection:
        def __init__(self):
            self.current = a
            self.commands = []

        def get_tree(self):
            focused = SimpleNamespace(workspace=lambda: self.current)
            return SimpleNamespace(
                find_focused=lambda: focused,
                workspaces=lambda: workspaces,
            )

        def command(self, command):
            self.commands.append(command)
            if command.startswith('workspace'):
                self.current = next(
                    ws for ws in workspaces
                    if command.endswith(main.quote_i3_string(ws.name))
                )

    connection = Connection()
    config = configparser.ConfigParser()
    config.read_dict({'CONF': {'live_preview_map_delay_sec': '0.0'}})
    monkeypatch.setattr(main, 'CONFIG', config, raising=False)
    monkeypatch.setattr(main, 'GLOBAL_UPDATES_RUNNING', False)
    monkeypatch.setattr(main.time, 'sleep', lambda _seconds: None)
    captured_exclusions = []

    def compose(ws, _previous, excluded_window_ids=None):
        captured_exclusions.append(excluded_window_ids)
        return ([
            ws.rect.width,
            ws.rect.height,
            bytearray(ws.rect.width * ws.rect.height * 3),
        ], 1)

    monkeypatch.setattr(main, 'compose_workspace_preview', compose)
    monkeypatch.setattr(main, 'update_tree_state', lambda _ws, _item: True)
    overview = {
        'con_id': 999,
        'window_id': 998,
        'workspace': 'a',
        'output': 'eDP-1',
    }

    assert main.refresh_live_workspace_previews(
        connection,
        ['a', 'b'],
        overview,
    )

    assert connection.commands == [
        'workspace --no-auto-back-and-forth "a"',
        'workspace --no-auto-back-and-forth "b"',
        'workspace --no-auto-back-and-forth "a"',
        '[con_id=999] focus',
    ]
    assert main.GLOBAL_KNOWLEDGE['wss']['a']['screenshot']
    assert main.GLOBAL_KNOWLEDGE['wss']['b']['screenshot']
    assert captured_exclusions == [{998}, {998}]
    assert main.GLOBAL_KNOWLEDGE['active'] == 'a'
    assert main.PREVIEW_SWEEP_RUNNING is False


def test_live_overview_becomes_full_output_sticky_curtain(monkeypatch):
    reset_knowledge()
    main.GLOBAL_KNOWLEDGE['active'] = 'a'
    config = configparser.ConfigParser()
    config.read_dict({'CONF': {'live_previews': 'true'}})
    commands = []
    overview_con = SimpleNamespace(id=999)
    output = SimpleNamespace(
        name='eDP-1',
        rect=SimpleNamespace(x=0, y=0, width=1920, height=1080),
    )

    class Connection:
        def get_tree(self):
            return SimpleNamespace(find_by_window=lambda window: overview_con)

        def get_outputs(self):
            return [output]

        def command(self, command):
            commands.append(command)
            return [SimpleNamespace(success=True)]

    monkeypatch.setattr(main, 'CONFIG', config, raising=False)
    monkeypatch.setattr(main, 'i3', Connection(), raising=False)
    monkeypatch.setattr(main, 'xcomposite_available', lambda: True)
    monkeypatch.setattr(main.pygame.display, 'get_wm_info', lambda: {'window': 555})
    monkeypatch.setattr(main.pygame.event, 'pump', lambda: None)
    monkeypatch.setattr(main.pygame.event, 'clear', lambda: None)
    monkeypatch.setattr(main.time, 'sleep', lambda _seconds: None)

    context = main.prepare_live_overview_window({
        'op': 'eDP-1',
        'x': 0,
        'y': 0,
        'w': 1920,
        'h': 1058,
    })

    assert context == {
        'con_id': 999,
        'window_id': 555,
        'workspace': 'a',
        'output': 'eDP-1',
    }
    assert commands == [
        '[con_id=999] fullscreen disable, floating enable, sticky enable, '
        'border pixel 0, resize set 1920 px 1080 px, '
        'move absolute position 0 px 0 px'
    ]


def test_new_window_event_queues_normal_refresh_and_auto_scan(monkeypatch):
    class Recorder:
        def __init__(self):
            self.calls = []

        def __call__(self, *args):
            self.calls.append(args)

    normal_refresh = Recorder()
    auto_scan = Recorder()
    config = configparser.ConfigParser()
    config.read_dict({'CONF': {'auto_scan_new_workspaces': 'true'}})
    connection = object()
    event = SimpleNamespace(change='new')

    monkeypatch.setattr(main, 'CONFIG', config, raising=False)
    monkeypatch.setattr(main, 'PREVIEW_SWEEP_RUNNING', False)
    monkeypatch.setattr(main, 'UPDATER_DEBOUNCED', normal_refresh, raising=False)
    monkeypatch.setattr(main, 'AUTO_SCAN_DEBOUNCED', auto_scan)

    main.on_win_new_or_move(connection, event)

    assert normal_refresh.calls == [(connection, event)]
    assert auto_scan.calls == [(connection,)]


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
    assert 'live_previews = true' in text
    assert 'toggle_shortcut = Mod4+e' in text
    assert config.get('CONF', 'bgcolor') == '#0A001F'

    lines = text.splitlines()
    documented_keys = (
        'bgcolor',
        'frame_active_color',
        'frame_inactive_color',
        'frame_missing_color',
        'tile_missing_color',
        'padding_percent_x',
        'padding_percent_y',
        'spacing_percent_x',
        'spacing_percent_y',
        'frame_width_px',
        'names_show',
        'names_font',
        'names_fontsize',
        'names_color',
        'highlight_percentage',
        'forced_update_interval_sec',
        'debounce_period_sec',
        'output_blacklist',
        'win_class_blacklist',
        'startup_scan',
        'auto_scan_new_workspaces',
        'new_workspace_scan_delay_sec',
        'workspace_capture_delay_sec',
        'live_previews',
        'live_preview_interval_sec',
        'live_preview_map_delay_sec',
        'toggle_shortcut',
        'store_state_on_restart',
        'max_persisted_state_age_sec',
        'log_lvl',
    )
    for key in documented_keys:
        index = next(
            index for index, line in enumerate(lines)
            if line.startswith(key + ' =')
        )
        comment = lines[index - 1]
        assert comment.startswith('# '), key
        assert any('\u4e00' <= char <= '\u9fff' for char in comment), key
        assert any('a' <= char.lower() <= 'z' for char in comment), key


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


def test_unedited_legacy_default_config_is_upgraded(tmp_path, monkeypatch):
    config_path = tmp_path / 'i3expo' / 'config'
    config_path.parent.mkdir()
    legacy = '# old generated template\n[CONF]\nbgcolor = #0A001F\n'
    config_path.write_text(legacy, encoding='utf-8')
    legacy_hash = main.hashlib.sha256(legacy.encode('utf-8')).hexdigest()
    config = configparser.ConfigParser(converters={'color': main.get_color})
    monkeypatch.setattr(main, 'CONFIG', config, raising=False)
    monkeypatch.setattr(main, 'CONFIG_FILE', str(config_path), raising=False)
    monkeypatch.setattr(main, 'LEGACY_DEFAULT_CONFIG_SHA256ES', {legacy_hash})

    main.read_config()

    upgraded = config_path.read_text(encoding='utf-8')
    assert upgraded.startswith('# i3expo 用户配置 / User configuration\n')
    assert '日志级别' in upgraded
    assert 'Log level' in upgraded


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


def test_destroyed_workspace_is_removed_from_overview():
    reset_knowledge()
    main.GLOBAL_KNOWLEDGE['wss'] = {
        'a': {'screenshot': [1], 'last-update': 10.0, 'state': 1},
        'k': {'screenshot': [2], 'last-update': 20.0, 'state': 2},
    }
    focused_ws = SimpleNamespace(name='a')
    focused = SimpleNamespace(workspace=lambda: focused_ws)
    tree = SimpleNamespace(
        workspaces=lambda: [focused_ws],
        find_focused=lambda: focused,
    )
    connection = SimpleNamespace(get_tree=lambda: tree)
    event = SimpleNamespace(change='empty')

    main.on_ws_empty(connection, event)

    assert list(main.GLOBAL_KNOWLEDGE['wss']) == ['a']
    assert main.GLOBAL_KNOWLEDGE['active'] == 'a'


def test_startup_prunes_destroyed_workspaces_from_saved_state(monkeypatch):
    a = workspace('a', -1, 10)
    focused = SimpleNamespace(workspace=lambda: a)
    tree = SimpleNamespace(
        workspaces=lambda: [a],
        find_focused=lambda: focused,
    )
    saved = {
        'active': 'l',
        'prev_f_w': None,
        'wss': {
            'a': {'name': 'a'},
            'l': {'name': 'l'},
        },
    }
    updated = []

    monkeypatch.setattr(main, 'load_global_knowledge', lambda: saved)
    monkeypatch.setattr(
        main,
        'i3',
        SimpleNamespace(get_tree=lambda: tree),
        raising=False,
    )
    monkeypatch.setattr(
        main,
        'update_workspace',
        lambda ws, focused_ws, hydration: updated.append(
            (ws.name, focused_ws.name, hydration)
        ),
    )

    main.init_knowledge()

    assert list(main.GLOBAL_KNOWLEDGE['wss']) == ['a']
    assert updated == [('a', 'a', True)]
