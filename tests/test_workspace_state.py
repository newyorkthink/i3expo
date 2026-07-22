from types import SimpleNamespace

from i3expo import main


def workspace(name, num, identifier, width=1920, height=1080):
    return SimpleNamespace(
        name=name,
        num=num,
        id=identifier,
        ipc_data={'output': 'eDP-1'},
        rect=SimpleNamespace(x=0, y=0, width=width, height=height),
        leaves=lambda: [],
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


def test_hydration_discards_preview_with_stale_dimensions():
    reset_knowledge()
    ws = workspace('a', -1, 10)
    main.GLOBAL_KNOWLEDGE['wss']['a'] = {
        'name': 'a',
        'screenshot': [800, 600, bytearray(1)],
        'last-update': 123.0,
        'state': 0,
        'w': 800,
        'h': 600,
    }

    item = main.update_workspace(ws, ws, hydration=True)

    assert item['screenshot'] == []
    assert item['last-update'] == 0.0
    assert (item['w'], item['h']) == (1920, 1080)


def test_force_refresh_bypasses_rate_limit():
    ws = workspace('1', 1, 10)
    item = {'last-update': 100.0, 'state': 0}

    assert main.should_update_ws(10.0, ws, item, 101.0, force=True)


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
