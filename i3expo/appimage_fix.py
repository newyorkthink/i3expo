"""AppImage runtime fixes for named i3 workspaces and bundled fonts."""

from __future__ import annotations

from typing import Any

from . import main


def _workspace_key(workspace: Any) -> str:
    """Use the workspace name as the stable key.

    i3 reports ``num == -1`` for every workspace whose name does not begin
    with a number. Keying by ``num`` therefore collapses names such as
    ``a``, ``k``, ``p`` and ``b`` into one entry.
    """

    return workspace.name


def update_workspace(workspace: Any, focused_workspace: Any, hydration: bool = False) -> dict:
    key = _workspace_key(workspace)
    item = main.GLOBAL_KNOWLEDGE["wss"].get(key)

    if item is None:
        item = main.GLOBAL_KNOWLEDGE["wss"][key] = {
            "op": workspace.ipc_data["output"],
            "name": workspace.name,
            "num": workspace.num,
            "id": workspace.id,
            "screenshot": [],
            "last-update": 0.0,
            "state": 0,
            "x": 0,
            "y": 0,
            "w": 0,
            "h": 0,
            "ratio": 0.0,
            "windows": {},
            "ff": None,
        }

    if hydration:
        item["id"] = workspace.id
        item["name"] = workspace.name
        item["num"] = workspace.num
    else:
        item["op"] = workspace.ipc_data["output"]
        item["name"] = workspace.name
        item["num"] = workspace.num
        item["id"] = workspace.id
        item["x"] = workspace.rect.x
        item["y"] = workspace.rect.y
        item["w"] = workspace.rect.width
        item["h"] = workspace.rect.height
        item["ratio"] = (
            workspace.rect.width / workspace.rect.height
            if workspace.rect.height
            else 1.0
        )

    if workspace.id == focused_workspace.id:
        main.GLOBAL_KNOWLEDGE["active"] = key

    return item


def init_knowledge() -> None:
    # The upstream persisted state uses numeric keys. Start clean so an old
    # ``-1`` entry cannot overwrite multiple named workspaces.
    main.GLOBAL_KNOWLEDGE = {
        "active": None,
        "prev_f_w": None,
        "wss": {},
    }

    tree = main.i3.get_tree()
    focused_workspace = tree.find_focused().workspace()

    for workspace in tree.workspaces():
        update_workspace(workspace, focused_workspace)


def on_ws(i3conn: Any, event: Any) -> None:
    main.LOGGER.debug(
        " ---- on ws state: %s, name: %s",
        event.change,
        event.current.name,
    )

    current_key = _workspace_key(event.current)
    item = main.GLOBAL_KNOWLEDGE["wss"].get(current_key)
    if item is not None:
        item["op"] = event.current.ipc_data["output"]

    if not main.GLOBAL_UPDATES_RUNNING:
        main.GLOBAL_UPDATES_RUNNING = True

        if event.old is not None:
            old_key = _workspace_key(event.old)
            old_item = main.GLOBAL_KNOWLEDGE["wss"].get(old_key)

            if old_item is not None and len(event.old.focus) > 1:
                window = i3conn.get_tree().find_by_id(event.old.focus[1])
                if window is not None and window.type == "floating_con" and window.focus:
                    old_item["ff"] = window.focus[0]

    elif item is not None and item["ff"] is not None and event.change == "focus":
        i3conn.command(f'[con_id={item["ff"]}] focus')
        item["ff"] = None

    main.WS_UPDATE_DEBOUNCED(
        i3conn,
        event,
        rate_limit_period=main.LOOP_INTERVAL,
        force=True,
        debounced=True,
    )


def on_ws_empty(i3conn: Any, event: Any) -> None:
    main.LOGGER.debug(" ---- on ws EMPTY: %s", event.change)
    workspace_names = {workspace.name for workspace in i3conn.get_tree().workspaces()}
    deleted = [
        key
        for key in main.GLOBAL_KNOWLEDGE["wss"]
        if key not in workspace_names
    ]

    for key in deleted:
        del main.GLOBAL_KNOWLEDGE["wss"][key]


def on_ws_rename(i3conn: Any, event: Any) -> None:
    main.LOGGER.debug(" ---- on ws RENAME: %s", event.change)

    old_name = event.old.name if event.old is not None else None
    new_name = event.current.name

    if old_name and old_name in main.GLOBAL_KNOWLEDGE["wss"]:
        item = main.GLOBAL_KNOWLEDGE["wss"].pop(old_name)
        item["name"] = new_name
        item["num"] = event.current.num
        main.GLOBAL_KNOWLEDGE["wss"][new_name] = item

        if main.GLOBAL_KNOWLEDGE["active"] == old_name:
            main.GLOBAL_KNOWLEDGE["active"] = new_name
    elif new_name in main.GLOBAL_KNOWLEDGE["wss"]:
        main.GLOBAL_KNOWLEDGE["wss"][new_name]["name"] = new_name


def draw_missing_tile(screen_width: int, screen_height: int):
    key = f"{screen_width}x{screen_height}"
    if key in main.qm_cache:
        return main.qm_cache[key]

    missing_tile = main.pygame.Surface(
        (screen_width, screen_height),
        main.pygame.SRCALPHA,
        32,
    )
    missing_tile = missing_tile.convert_alpha()

    # Use pygame's bundled default font. This avoids depending on a system
    # font alias such as ``sans-serif`` inside the AppImage.
    font = main.pygame.font.Font(None, max(12, screen_height))
    question_mark = font.render("?", True, (150, 150, 150))
    question_size = question_mark.get_rect().size
    origin_x = round((screen_width - question_size[0]) / 2)
    origin_y = round((screen_height - question_size[1]) / 2)
    missing_tile.blit(question_mark, (origin_x, origin_y))

    main.qm_cache[key] = missing_tile
    return missing_tile


def install() -> None:
    main.update_workspace = update_workspace
    main.init_knowledge = init_knowledge
    main.on_ws = on_ws
    main.on_ws_empty = on_ws_empty
    main.on_ws_rename = on_ws_rename
    main.draw_missing_tile = draw_missing_tile
