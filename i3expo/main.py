#!/usr/bin/python3
#
# dependencies:
#    pip3 install --user -r ./requirements.txt
#
# add i3 conf:
#   exec_always --no-startup-id i3expo
#   for_window [class="^i3expo$"] fullscreen enable
#   bindsym $mod1+e exec --no-startup-id killall -s SIGUSR1 i3expo

import os
import sys
import configparser
os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = '1'  # needs to be set prior to importing pygame; see https://github.com/pygame/pygame/issues/1468
os.environ.setdefault('SDL_VIDEO_X11_WMCLASS', 'i3expo')
import pygame
import i3ipc
import copy
import signal
import traceback
import pprint
import time
import math
import logging
import tempfile
import warnings
from .debounce import Debounce
from functools import partial
from threading import Thread
from PIL import Image, ImageDraw
import pulp
from pulp import PULP_CBC_CMD
import ctypes
import pickle
from datetime import datetime
from tendo import singleton
# import prtscn_py

from xdg.BaseDirectory import xdg_config_home

SELF_WIN_CLASS = 'i3expo'
pp = pprint.PrettyPrinter(indent=4)

GLOBAL_UPDATES_RUNNING = True  # if false, we don't grab any screenshots/update internal state
PREVIEW_SWEEP_RUNNING = False  # true while unseen workspaces are visited for their first preview
SHORTCUT_THREAD = None

qm_cache = {}  # screen_w x screen_h mapped against rendered question mark for missing tiles
LOCK = None
LOGGER = logging.getLogger(__name__)

DEFAULT_CONFIG_TEMPLATE = '''# i3expo user configuration
# Send SIGHUP to reload theme/layout values; restart after changing shortcut.
[CONF]
bgcolor = #0A001F
frame_active_color = #00D7FF
frame_inactive_color = #00D7FF
frame_missing_color = #00D7FF
tile_missing_color = #0A001F

padding_percent_x = 5
padding_percent_y = 5
spacing_percent_x = 4
spacing_percent_y = 4
frame_width_px = 3

names_show = true
# "default" is pygame's bundled font and is available inside the AppImage.
names_font = default
names_fontsize = 25
names_color = #FF5FFF
highlight_percentage = 20

forced_update_interval_sec = 10.0
debounce_period_sec = 1.0
output_blacklist =
win_class_blacklist = i3expo

# Automatically visit every non-empty workspace once at startup so X11 can
# render and refresh it. A small visible switch is unavoidable.
startup_scan = true
workspace_capture_delay_sec = 0.2

# Global X11 shortcut. Examples: Mod4+e, Alt+Tab, Ctrl+Shift+space.
# Leave empty to disable it and use an i3 bindsym instead.
toggle_shortcut = Mod4+e

store_state_on_restart = true
max_persisted_state_age_sec = 604800
log_lvl = INFO
'''


# def _runtime_path() -> str:
    # return  xdg.BaseDirectory.get_runtime_dir()

def _runtime_path() -> str:
    uid = os.getuid()
    runtime_roots = [
        os.environ.get('XDG_RUNTIME_DIR'),
        f'/run/user/{uid}',
        tempfile.gettempdir(),
    ]

    for root in runtime_roots:
        if not root:
            continue
        dirname = 'i3expo' if root != tempfile.gettempdir() else f'i3expo-{uid}'
        dir_path = os.path.join(root, dirname)
        try:
            os.makedirs(dir_path, mode=0o700, exist_ok=True)
            if os.access(dir_path, os.W_OK | os.X_OK):
                return dir_path
        except OSError:
            continue

    raise RuntimeError('Unable to create a writable i3expo runtime directory')

RUNTIME_PATH = _runtime_path()

def shutdown_common():
    global GLOBAL_UPDATES_RUNNING

    LOGGER.info('Shutting down...')

    try:
        if (
            'GLOBAL_KNOWLEDGE' in globals()
            and 'CONFIG' in globals()
            and CONFIG.getboolean('CONF', 'store_state_on_restart')
        ):
            persist_state()
        GLOBAL_UPDATES_RUNNING = False
        if 'UPDATER_DEBOUNCED' in globals():
            UPDATER_DEBOUNCED.reset()
        if 'WS_UPDATE_DEBOUNCED' in globals():
            WS_UPDATE_DEBOUNCED.reset()
        if 'i3' in globals():
            i3.main_quit()

        pygame.display.quit()
        pygame.quit()
    except Exception as e:
        LOGGER.error('exception on shutdown:')
        LOGGER.error(e)
    finally:
        os._exit(0)


def signal_quit(signal, stack_frame):
    # i3.main_quit()
    shutdown_common()


def load_global_knowledge() -> dict:
    default = {'active': None,  # currently active workspace name
               'prev_f_w': None,
               'wss': {}
              }

    state_f = CONFIG.get('CONF', 'state_f')
    if not (os.path.isfile(state_f) and os.access(state_f, os.R_OK)):
        return default

    try:
        with open(state_f, 'rb') as f:
            s = pickle.load(f)
            t = s.get('timestamp', 0)

            if (_unix_time_now() - t <= CONFIG.getint('CONF', 'max_persisted_state_age_sec')):
                loaded = s.get('gknowledge', default)
                normalized = default.copy()
                normalized['prev_f_w'] = loaded.get('prev_f_w')

                # State written by releases before appimage.3 used workspace
                # numbers as keys. Re-key by the stored name so old state can
                # be migrated without collapsing every named workspace at -1.
                for item in loaded.get('wss', {}).values():
                    name = item.get('name')
                    if name:
                        normalized['wss'][str(name)] = item
                return normalized
    except Exception as e:
        LOGGER.error(e)
    return default


def persist_state():
    global GLOBAL_UPDATES_RUNNING

    GLOBAL_UPDATES_RUNNING = False

    try:
        # pp.pprint(GLOBAL_KNOWLEDGE)
        for v in GLOBAL_KNOWLEDGE['wss'].values():
            i = v['screenshot']
            # in order to pickle ctypes data, convert it into bytearray:
            if i:
                v['screenshot'][2] = bytearray(i[2])

        data = {
                'timestamp':  _unix_time_now(),
                'gknowledge': GLOBAL_KNOWLEDGE
               }

        with open(CONFIG.get('CONF', 'state_f'), 'wb') as f:
            pickle.dump(data, f)
    except Exception as e:
        LOGGER.error(e)


def _unix_time_now() -> int:
    return int(datetime.now().timestamp())


def on_shutdown(i3conn, e):
    shutdown_common()


def signal_reload(signal, stack_frame):
    hot_reload()


def hot_reload():
    global LOOP_INTERVAL
    global OUTPUT_BLACKLIST
    global WIN_CLASS_BLACKLIST
    global GRAB

    read_config()

    log_lvl = getattr(logging, CONFIG.get('CONF', 'log_lvl'))
    logging.basicConfig(stream=sys.stdout, level=log_lvl, force=True)

    # TODO: compare previous & new output_blacklist & update ws knowledge!

    # re-define global vars populated from CONFIG:
    LOOP_INTERVAL = CONFIG.getfloat('CONF', 'forced_update_interval_sec')
    OUTPUT_BLACKLIST = [x.strip() for x in CONFIG.get('CONF', 'output_blacklist').split(',') if x and x.strip()]
    WIN_CLASS_BLACKLIST = [x.strip() for x in CONFIG.get('CONF', 'win_class_blacklist').split(',') if x and x.strip()]
    if SELF_WIN_CLASS not in WIN_CLASS_BLACKLIST:
        WIN_CLASS_BLACKLIST.append(SELF_WIN_CLASS)

    screenshot_lib_path = CONFIG.get('CONF', 'screenshot_lib_path')
    GRAB = ctypes.CDLL(screenshot_lib_path)
    GRAB.getScreen.argtypes = []


def shown_ws():
    focused_op = i3.get_tree().find_focused().workspace().ipc_data['output']

    # LOGGER.debug(f"global_knowledge: {[type(i['op']) for i in GLOBAL_KNOWLEDGE['wss'].values()]}")
    # LOGGER.debug(f"global_knowledge: {GLOBAL_KNOWLEDGE['wss']}")
    return [k for k, v in GLOBAL_KNOWLEDGE['wss'].items()
            if v['op'] not in OUTPUT_BLACKLIST or v['op'] == focused_op]


def signal_toggle_ui(signal, stack_frame):
    global GLOBAL_UPDATES_RUNNING

    if not GLOBAL_UPDATES_RUNNING:  # UI toggle
        GLOBAL_UPDATES_RUNNING = 1  # make sure UI gets closed on workspace switch (including if we move cursor to neighboring WS when UI is rendered)
                                    # note int type is to signify special condition to input_event_loop().
        return

    wss = shown_ws()
    if len(wss) > 1:  # i.e. should show UI
        capture_missing_workspace_previews(
            i3,
            wss,
            CONFIG.getfloat('CONF', 'workspace_capture_delay_sec'),
        )
        wss = shown_ws()
        # Refresh the visible workspace immediately before drawing the grid;
        # missing non-empty workspaces were mapped and captured just above.
        update_state(i3, force=True)
        # i3.command('workspace i3expo-temporary-workspace')  # jump to temp ws; doesn't seem to work well in multimon setup; introduced by  https://gitlab.com/d.reis/i3expo/-/commit/d14685d16fd140b3a7374887ca086ea66e0388f5 - looks like it solves problem where fullscreen state is lost on expo toggle
        GLOBAL_UPDATES_RUNNING = False
        UPDATER_DEBOUNCED.reset()
        WS_UPDATE_DEBOUNCED.reset()

        # ui_thread = Thread(target = show_ui)
        # ui_thread.daemon = True
        try:
            show_ui(wss)
        except Exception:
            LOGGER.exception('Unable to display workspace overview')
            GLOBAL_UPDATES_RUNNING = True


def get_color(raw):
    return pygame.Color(raw)


def read_config():
    CONFIG.read_dict({
        'CONF': {
            'bgcolor'                    : '#0A001F',
            'frame_active_color'         : '#00D7FF',
            'frame_inactive_color'       : '#00D7FF',
            'frame_missing_color'        : '#00D7FF',
            'tile_missing_color'         : '#0A001F',

            'padding_percent_x'          : 5,
            'padding_percent_y'          : 5,
            'spacing_percent_x'          : 4,
            'spacing_percent_y'          : 4,
            'frame_width_px'             : 3,

            'forced_update_interval_sec' : 10.0,
            'debounce_period_sec'        : 1.0,
            'output_blacklist'           : '',  # comma-separated values as a string; empty string for none
            'win_class_blacklist'        : SELF_WIN_CLASS,  # comma-separated values as a string; empty string for none

            'names_show'                 : True,
            'names_font'                 : 'default',
            'names_fontsize'             : 25,
            'names_color'                : '#FF5FFF',
            'highlight_percentage'       : 20,
            'startup_scan'               : True,
            'workspace_capture_delay_sec': 0.2,
            'toggle_shortcut'            : 'Mod4+e',
            'screenshot_lib_path'        : os.path.join(os.path.dirname(os.path.realpath(__file__)), 'prtscn.so'),
            'store_state_on_restart'     : True,
            'max_persisted_state_age_sec': 604800,
            'state_f'                    : f'{RUNTIME_PATH}/{SELF_WIN_CLASS}.state',
            'log_lvl'                    : 'INFO'
        }
    })

    if not os.path.exists(CONFIG_FILE):
        try:
            os.makedirs(os.path.dirname(CONFIG_FILE), mode=0o700, exist_ok=True)
            with open(CONFIG_FILE, 'x', encoding='utf-8') as config_file:
                config_file.write(DEFAULT_CONFIG_TEMPLATE)
        except FileExistsError:
            pass
        except OSError:
            LOGGER.exception('Unable to create default config at %s', CONFIG_FILE)

    if os.path.exists(CONFIG_FILE):
        CONFIG.read(CONFIG_FILE)


def grab_screen(i):
    # LOGGER.debug('GRABBING FOR: {}'.format(i['name']))
    w = i['w']
    h = i['h']

    result = (ctypes.c_ubyte * w * h * 3)()  # *3 for R,G,B
    GRAB.getScreen(i['x'], i['y'], w, h, result)
    return [w, h, result]


def screenshot_is_valid(screenshot) -> bool:
    """Return whether a persisted screenshot has a complete RGB buffer."""
    try:
        w, h, pixels = screenshot
        try:
            buffer_size = ctypes.sizeof(pixels)
        except TypeError:
            buffer_size = len(pixels)
        return w > 0 and h > 0 and buffer_size == w * h * 3
    except (TypeError, ValueError):
        return False


def quote_i3_string(value) -> str:
    """Quote a workspace name for use in an i3 command."""
    value = str(value).replace('\\', '\\\\').replace('"', '\\"')
    return '"{}"'.format(value)


def wait_for_workspace(i3conn, name, timeout=1.5):
    """Wait until i3 has focused ``name`` and return its live tree objects."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        tree = i3conn.get_tree()
        focused_con = tree.find_focused()
        if focused_con is not None:
            focused_ws = focused_con.workspace()
            if focused_ws is not None and focused_ws.name == name:
                return tree, focused_con, focused_ws
        time.sleep(0.05)
    return None


def capture_missing_workspace_previews(
    i3conn,
    workspace_keys,
    capture_delay=0.2,
    force=False,
):
    """Visit non-empty workspaces and cache missing or forced screenshots.

    X11 cannot capture an i3 workspace which has never been mapped.  The sweep
    therefore switches only to live workspaces that have windows and no cached
    preview, then restores the exact container which was focused beforehand.
    """
    global PREVIEW_SWEEP_RUNNING

    tree = i3conn.get_tree()
    original_con = tree.find_focused()
    if original_con is None or original_con.workspace() is None:
        return

    original_ws = original_con.workspace()
    original_con_id = original_con.id
    requested = set(workspace_keys)
    targets = [
        ws for ws in tree.workspaces()
        if (
            workspace_key(ws) in requested
            and workspace_key(ws) != workspace_key(original_ws)
            and ws.leaves()
            and (
                force
                or not GLOBAL_KNOWLEDGE['wss'][workspace_key(ws)]['screenshot']
            )
        )
    ]
    if not targets:
        return

    PREVIEW_SWEEP_RUNNING = True
    UPDATER_DEBOUNCED.reset()
    WS_UPDATE_DEBOUNCED.reset()
    try:
        for target in targets:
            name = target.name
            try:
                i3conn.command(
                    'workspace --no-auto-back-and-forth {}'.format(
                        quote_i3_string(name)
                    )
                )
                focused = wait_for_workspace(i3conn, name)
                if focused is None:
                    LOGGER.warning(
                        'Timed out while preparing preview for workspace %s', name
                    )
                    continue

                # Give applications one normal frame to repaint after mapping.
                time.sleep(max(0.0, capture_delay))
                tree, focused_con, focused_ws = (
                    wait_for_workspace(i3conn, name) or focused
                )
                wk = update_workspace(focused_ws, focused_ws)
                wk['screenshot'] = grab_screen(wk)
                wk['last-update'] = time.time()
                update_tree_state(focused_ws, wk)
            except Exception:
                LOGGER.exception('Unable to prepare preview for workspace %s', name)
    finally:
        try:
            i3conn.command('[con_id={}] focus'.format(original_con_id))
            restored = wait_for_workspace(i3conn, original_ws.name)
            if restored is None:
                i3conn.command(
                    'workspace --no-auto-back-and-forth {}'.format(
                        quote_i3_string(original_ws.name)
                    )
                )
                restored = wait_for_workspace(i3conn, original_ws.name)
            if restored is not None:
                tree, focused_con, focused_ws = restored
                update_workspace(focused_ws, focused_ws)
        except Exception:
            LOGGER.exception(
                'Unable to restore workspace %s after preview capture',
                original_ws.name,
            )
        finally:
            PREVIEW_SWEEP_RUNNING = False


def workspace_key(ws) -> str:
    """Return the only collision-free i3 workspace identifier.

    i3 reports ``num == -1`` for every workspace whose name does not begin
    with a number, so ``ws.num`` cannot be used as a dictionary key.
    """
    return str(ws.name)


def update_workspace(ws, focused_ws, hydration=False) -> dict:
    key = workspace_key(ws)
    i = GLOBAL_KNOWLEDGE['wss'].get(key)
    if i is None:
        i = GLOBAL_KNOWLEDGE['wss'][key] = {
            'op'          : ws.ipc_data['output'],
            'name'        : ws.name,
            'num'         : ws.num,
            'id'          : ws.id,
            'screenshot'  : [],    # array of [w,h,byte-array representation of this ws screenshot]
            'last-update' : 0.0,   # unix epoch when ws was last grabbed
            'state'       : 0,     # numeric representation of current state of ws - windows and their sizes/is focused et al
            'x'           : 0,
            'y'           : 0,
            'w'           : 0,
            'h'           : 0,
            'ratio'       : 0.0,
            'windows'     : {},    # TODO unused atm
            'ff'          : None   # float-focus; ID of a floating window to focus when we return to this WS
        }

    if hydration and i.get('screenshot') and not screenshot_is_valid(i['screenshot']):
        # Ignore corrupt/incomplete cache data, but keep valid previews when the
        # bar or output dimensions changed: pygame safely scales those images.
        i['screenshot'] = []
        i['last-update'] = 0.0

    # Always refresh live metadata. Cached screenshots keep their own dimensions
    # and can be scaled even when a panel changed the live workspace rectangle.
    i['op'] = ws.ipc_data['output']
    i['name'] = ws.name
    i['num'] = ws.num
    i['id'] = ws.id
    i['x'] = ws.rect.x
    i['y'] = ws.rect.y
    i['w'] = ws.rect.width
    i['h'] = ws.rect.height
    i['ratio'] = ws.rect.width / ws.rect.height if ws.rect.height else 1.0

    if ws.id == focused_ws.id:
        GLOBAL_KNOWLEDGE['active'] = key
    return i


def init_knowledge():
    global GLOBAL_KNOWLEDGE

    GLOBAL_KNOWLEDGE = load_global_knowledge()
    state_hydration = bool(GLOBAL_KNOWLEDGE['wss'])

    tree = i3.get_tree()
    focused_ws = tree.find_focused().workspace()

    for ws in tree.workspaces():
        # LOGGER.debug('workspaces() num {} name [{}], focused {}'.format(ws.num, ws.name, ws.focused))
        update_workspace(ws, focused_ws, state_hydration)


# TODO: instead of querying i3.get_outputs(), consider subscribing to 'output' event
#       and maintaining internal output list/state
def get_all_active_workspaces(i3, focused_ws):
    return [output.current_workspace for output in i3.get_outputs()
            # if output.active and output.name not in OUTPUT_BLACKLIST]
            if output.active and (focused_ws.name == output.current_workspace or output.name not in OUTPUT_BLACKLIST)]


# ! Note calling this function will also store the current state in GLOBAL_KNOWLEDGE!
# TODO: this will likely be deprecated when/if i3 implements 'resize' event. actually... we now also track window title changes.
def update_tree_state(ws, wk):
    state = 0
    for con in ws.leaves():
        f = 31 if con.focused else 0  # so window focus change can be detected
        # add following if you want window title to be included in the state:
        # abs(hash(con.name)) % 10_000
        # or: hash(con.name) % 10_000  (if neg values are ok)
        state += con.id % (con.rect.x + con.rect.y + con.rect.width +
                           con.rect.height + hash(con.name) % 10_000 + f)

    if wk['state'] == state:
        return False
    wk['state'] = state
    return True


def should_update_ws(rate_limit_period, ws, wk, t, force):
    if not force and rate_limit_period is not None and t - wk['last-update'] <= rate_limit_period:
        return False
    return update_tree_state(ws, wk) or force


def update_state(i3, e=None, rate_limit_period=None,
                 force=False, debounced=False,
                 all_active_ws=False):
    LOGGER.debug('[ TOGGLING updat_state(){}; force: {}, debounced: {}'.format(' by event [' + e.change + ']' if e else '', force, debounced))

    time.sleep(0.2)  # TODO system-specific; configurize? also, maybe only sleep if it's _not_ debounced?

    tree = i3.get_tree()
    focused_con = tree.find_focused()

    if (PREVIEW_SWEEP_RUNNING or
        not GLOBAL_UPDATES_RUNNING or
        focused_con.window_class in WIN_CLASS_BLACKLIST):  # note assumes WindowEvent
            LOGGER.debug('] update skipped')
            UPDATER_DEBOUNCED.reset()
            WS_UPDATE_DEBOUNCED.reset()
            return

    t0 = time.time()
    focused_ws = focused_con.workspace()

    UPDATER_DEBOUNCED.reset()
    WS_UPDATE_DEBOUNCED.reset()

    if all_active_ws:
        active_ws_list = get_all_active_workspaces(i3, focused_ws)
        wss = [ws for ws in tree.workspaces() if ws.name in active_ws_list]
    else:  # update/process only the currently focused ws
        wss = [focused_ws]

    # either use our legacy grabbing logic...: {
    for ws in wss:
        wk = update_workspace(ws, focused_ws)
        if should_update_ws(rate_limit_period, ws, wk, t0, force):
            t1 = time.time()
            wk['screenshot'] = grab_screen(wk)
            LOGGER.debug('  -> grabbing WS {} image took {}'.format(ws.name, time.time()-t1))
            wk['last-update'] = t0

    # } ...or new py-bindings: {  # this seems to be slower, for whatever the reason
    # params = []
    # ws_to_process = []
    # for ws in wss:
        # update_workspace(ws, focused_ws)
        # if should_update_ws(rate_limit_period, ws, force):
            # i = GLOBAL_KNOWLEDGE['wss'][ws.num]
            # params += [i['x'], i['y'], i['w'], i['h']]
            # ws_to_process.append(i)

    # if params:
        # t1 = time.time()
        # screenshots = prtscn_py.get_screens(*params)
        # LOGGER.debug('  -> grabbing {} WS{} took {}'.format(len(ws_to_process), 'es' if len(ws_to_process) > 1 else '', time.time()-t1))

        # for idx, ws_state in enumerate(ws_to_process):
            # ws_state['screenshot'] = screenshots[idx]
            # ws_state['last-update'] = t0
    # }

    LOGGER.debug('] whole update_state() took {}'.format(time.time()-t0))


def get_hovered_tile(mpos, tiles):
    for tile_idx, t in tiles.items():
        if (mpos[0] >= t['ul'][0]
                and mpos[0] <= t['br'][0]
                and mpos[1] >= t['ul'][1]
                and mpos[1] <= t['br'][1]):
            return tile_idx
    return None


def show_ui(wss):
    global GLOBAL_UPDATES_RUNNING

    pre_expo_focused_win_id = i3.get_tree().find_focused().id

    frame_active_color = CONFIG.getcolor('CONF', 'frame_active_color')
    frame_inactive_color = CONFIG.getcolor('CONF', 'frame_inactive_color')
    frame_missing_color = CONFIG.getcolor('CONF', 'frame_missing_color')
    tile_missing_color = CONFIG.getcolor('CONF', 'tile_missing_color')

    pygame.display.init()
    pygame.font.init()

    ws = GLOBAL_KNOWLEDGE['wss'][GLOBAL_KNOWLEDGE['active']]

    with warnings.catch_warnings():
        warnings.filterwarnings(
            'ignore',
            message='Requested window was forcibly resized by the OS.*',
            category=RuntimeWarning,
        )
        screen = pygame.display.set_mode(
            (ws['w'], ws['h']),
            pygame.RESIZABLE | pygame.NOFRAME,
        )
    pygame.display.set_caption(SELF_WIN_CLASS)

    tiles = {}  # contains grid tile index to thumbnail/ws_screenshot data mappings
    active_tile = None

    wss2 = list(wss)  # shallow copy

    grid_layout = resolve_grid_layout(ws['w'], ws['h'], wss2)

    grid = []

    # compose the grid:
    for row_idx, elements_in_row in enumerate(grid_layout):
        no_of_previous_tiles = sum(grid_layout[:row_idx])
        row = []
        grid.append(row)

        for curr_tile_on_row_idx in range(elements_in_row):
            index = no_of_previous_tiles + curr_tile_on_row_idx
            ws_num = wss.pop(0)
            t = {
                'active'    : False,
                'mouseoff'  : None,
                'mouseon'   : None,
                'ul'        : (-1, -1),  # upper-left coords (including frame/border);
                'br'        : (-1, -1),  # bottom-right coords (including frame/border);
                'row_idx'   : row_idx,
                'ws'        : ws_num,        # workspace-name key represented by this tile;
                'frame_col' : None,
                'tile_col'  : None,
                'img'       : None  # processed, ie pygame-ready thumbnail;
            }
            tiles[index] = t
            row.append(t)

            ws_conf = GLOBAL_KNOWLEDGE['wss'][ws_num]

            if ws_conf['screenshot']:
                # t0 = time.time()
                t['img'] = process_img(ws_conf['screenshot'])
                # LOGGER.debug('processing image took {}'.format(time.time()-t0))
                if GLOBAL_KNOWLEDGE['active'] == ws_num:
                    active_tile = index  # first highlight our current ws
                    t['frame_col'] = frame_active_color
                else:
                    t['frame_col'] = frame_inactive_color
            else:
                t['frame_col'] = frame_missing_color
                t['tile_col'] = tile_missing_color
                t['img'] = draw_missing_tile(ws_conf['w'], ws_conf['h'])


    draw_grid(screen, grid)
    pygame.display.flip()  # update full dispaly Surface on the screen
    i = input_event_loop(screen, tiles, active_tile, grid_layout, wss2)
    pygame.display.quit()
    pygame.quit()

    # restore focus on previously focused window if we didn't switch WS; otherwise
    # the focus will likely be stolen from a floating/stacked window:
    if i is not None:  # TODO: this additional check is not needed, right?:   and i3.get_tree().find_focused().workspace().num == GLOBAL_KNOWLEDGE['active']:
        i3.command('[con_id={}] focus'.format(pre_expo_focused_win_id))

    GLOBAL_UPDATES_RUNNING = True  # should be set before we send 'workspace' cmd! this way on_ws() won't set GLOBAL_UPDATES_RUNNING again
    if isinstance(i, str):
        i3.command(i)


def process_img(shot):
    pil = Image.frombuffer('RGB', (shot[0], shot[1]), shot[2], 'raw', 'RGB', 0, 1)
    # return pygame.image.fromstring(pil.tobytes(), pil.size, pil.mode)
    return pygame.image.frombuffer(pil.tobytes(), pil.size, pil.mode)  # frombuffer() potentially faster than .fromstring()


def get_font(name, size):
    """Load a requested system font without pygame's noisy fallback warning."""
    if not name or name.strip().lower() in ('default', 'pygame'):
        return pygame.font.Font(None, size)
    font_path = pygame.font.match_font(name) if name else None
    return pygame.font.Font(font_path, size) if font_path else pygame.font.Font(None, size)


def draw_missing_tile(screen_w, screen_h):
    font_name = CONFIG.get('CONF', 'names_font')
    color = CONFIG.getcolor('CONF', 'names_color')
    key = '{}x{}-{}-{}'.format(screen_w, screen_h, font_name, color)

    if key in qm_cache:
        return qm_cache[key]

    missing_tile = pygame.Surface((screen_w, screen_h), pygame.SRCALPHA, 32)
    missing_tile = missing_tile.convert_alpha()
    qm = get_font(font_name, max(12, screen_h)).render('?', True, color)
    qm_size = qm.get_rect().size
    origin_x = round((screen_w - qm_size[0])/2)
    origin_y = round((screen_h - qm_size[1])/2)
    missing_tile.blit(qm, (origin_x, origin_y))

    qm_cache[key] = missing_tile

    return missing_tile


def get_max_tile_dimensions(screen_w, screen_h, pad_w, pad_h, spacing_x, spacing_y, grid):
    # if (screen_w > screen_h):  # TODO
    max_row_len = max([len(row) for row in grid])
    r = screen_h / screen_w

    # find tile width:
    problem = pulp.LpProblem('optimalTileWidth', pulp.LpMaximize)
    max_tile_w = pulp.LpVariable('max_tile_w', lowBound = 0)
    problem += max_tile_w
    problem += r * len(grid) * max_tile_w + (len(grid)-1) * spacing_y + 2*pad_h <= screen_h
    problem += max_row_len*max_tile_w + (max_row_len-1)*spacing_x + 2*pad_w <= screen_w

    result = problem.solve(PULP_CBC_CMD(msg=False))
    assert result == pulp.LpStatusOptimal
    max_tile_w = max_tile_w.value()
    max_tile_h = max_tile_w * r

    return max_tile_w, max_tile_h


def render_workspace_name(tile, screen, origin_x, origin_y, tile_w, tile_h):
    try:
        # check if name for given ws has been hardcoded in our CONFIG:
        name = CONFIG.get('CONF', 'workspace_' + str(tile['ws']))
    except Exception:
        name = GLOBAL_KNOWLEDGE['wss'][tile['ws']]['name']

    highlight_percentage = CONFIG.getint('CONF', 'highlight_percentage')
    names_color = CONFIG.getcolor('CONF', 'names_color')
    names_font = CONFIG.get('CONF', 'names_font')
    names_fontsize = CONFIG.getint('CONF', 'names_fontsize')
    font = get_font(names_font, names_fontsize)

    name = font.render(name, True, names_color)
    name_width = name.get_rect().size[0]
    name_x = origin_x + round((tile_w - name_width) / 2)
    name_y = origin_y + round(tile_h) + round(tile_h * 0.02)
    screen.blit(name, (name_x, name_y))


def draw_grid(screen, grid):
    padding_x = CONFIG.getint('CONF', 'padding_percent_x')
    padding_y = CONFIG.getint('CONF', 'padding_percent_y')
    spacing_x = CONFIG.getint('CONF', 'spacing_percent_x')
    spacing_y = CONFIG.getint('CONF', 'spacing_percent_y')
    frame_width = CONFIG.getint('CONF', 'frame_width_px')
    highlight_percentage = CONFIG.getint('CONF', 'highlight_percentage')

    screen_w = screen.get_width()
    screen_h = screen.get_height()

    pad_w = screen_w * padding_x / 100  # spacing between outermost tiles and screen
    pad_h = screen_h * padding_y / 100  # spacing between outermost tiles and screen
    spacing_x = screen_w * spacing_x / 100  # spacing between tiles
    spacing_y = screen_h * spacing_y / 100  # spacing between tiles


    screen.fill(CONFIG.getcolor('CONF', 'bgcolor'))

    max_tile_w, max_tile_h = get_max_tile_dimensions(screen_w, screen_h, pad_w, pad_h, spacing_x, spacing_y, grid)

    for i, row in enumerate(grid):
        # origin_y = round((screen_h - len(grid)*max_tile_h - (len(grid)-1)*spacing_y)/2) + round((max_tile_h + spacing_y) * i)
        center_y = ((screen_h - len(grid)*max_tile_h - (len(grid)-1)*spacing_y)/2) + ((max_tile_h + spacing_y) * i) + max_tile_h/2
        for j, t in enumerate(row):

            tile_h = max_tile_h  # reset
            tile_w = max_tile_w  # reset

            # origin_x = round((screen_w - len(row)*max_tile_w - (len(row)-1)*spacing_x)/2) + round((max_tile_w + spacing_x) * j)
            center_x = ((screen_w - len(row)*max_tile_w - (len(row)-1)*spacing_x)/2) + ((max_tile_w + spacing_x) * j) + max_tile_w/2

            ws_conf = GLOBAL_KNOWLEDGE['wss'][t['ws']]

            if (screen_w > screen_h):
                # height remains @ max
                tile_w = tile_h * ws_conf['ratio']
            else:
                # width remains @ max
                tile_h = tile_w / ws_conf['ratio']

            tile_w_rounded = round(tile_w)
            tile_h_rounded = round(tile_h)
            origin_y = center_y - tile_h/2
            origin_x = center_x - tile_w/2


            t['ul'] = (origin_x, origin_y)
            t['br'] = (origin_x + tile_w_rounded, origin_y + tile_h_rounded)

            screen.fill(t['frame_col'],
                    (
                        origin_x,
                        origin_y,
                        tile_w,
                        tile_h
                    ))
            if t['tile_col'] is not None:
                screen.fill(t['tile_col'],
                        (
                            origin_x + frame_width,
                            origin_y + frame_width,
                            tile_w - 2*frame_width,
                            tile_h - 2*frame_width
                        ))

            # draw ws thumbnail (note we need to adjust for frame/border width)
            screen.blit(
                    pygame.transform.smoothscale(t['img'], (tile_w_rounded - 2*frame_width, tile_h_rounded - 2*frame_width)),
                    (origin_x + frame_width, origin_y + frame_width)
            )

            if CONFIG.getboolean('CONF', 'names_show'):
                render_workspace_name(t, screen, origin_x, origin_y, tile_w, tile_h)

            mouseoff = screen.subsurface((origin_x, origin_y, tile_w_rounded, tile_h_rounded)).copy()  # used to replace mouseon highlight
            mouseon = mouseoff.copy()

            lightmask = pygame.Surface((tile_w_rounded, tile_h_rounded), pygame.SRCALPHA, 32)
            lightmask.convert_alpha()
            lightmask.fill((255,255,255,255 * highlight_percentage / 100))
            mouseon.blit(lightmask, (0, 0))
            t['mouseon'] = mouseon
            t['mouseoff'] = mouseoff


def resolve_grid_layout(screen_w, screen_h, wss) -> list[int]:
    grid = []
    max_tiles_per_row = 3 if screen_w >= screen_h else 2  # TODO: resolve from ratio?

    # TODO: need to start increasing max_nr_per_row as well from here?
    l = len(wss)
    rows = math.ceil(l/max_tiles_per_row)
    while rows > 0:
        tiles_on_row = math.ceil(l/rows)
        grid.append(tiles_on_row)
        l -= tiles_on_row
        rows -= 1

    return grid


def direct_workspace_command(key, tiles):
    """Return an i3 command when a typed key exactly names a workspace."""
    if len(key) != 1:
        return None
    for tile in tiles.values():
        name = str(GLOBAL_KNOWLEDGE['wss'][tile['ws']]['name'])
        if name == key:
            return 'workspace --no-auto-back-and-forth {}'.format(
                quote_i3_string(name)
            )
    return None


# return of not-None means we should focus the
# previously-focused window upon the closure of expo UI
#
# and only str output should mean returned WS-num should be focused
def input_event_loop(screen, tiles, active_tile, grid, wss):
    t1 = time.time()
    workspaces = len(wss)

    while pygame.display.get_init():  # Returns True if the display module has been initialized
        if GLOBAL_UPDATES_RUNNING:
            # if GLOBAL_UPDATES_RUNNING is True:
            if isinstance(GLOBAL_UPDATES_RUNNING, bool):  # note bools are subclass of int, but not the other way around
                return None
            return 1  # not string nor None

        is_mouse_input = False
        kbdmove = None
        jump = False   # states whether we're navigating into a selected ws

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return 1  # not string nor None
            elif event.type == pygame.MOUSEMOTION:
                is_mouse_input = True
            elif event.type == pygame.KEYDOWN:
                command = direct_workspace_command(event.unicode, tiles)
                if command is not None:
                    return command
                if event.key == pygame.K_UP or event.key == pygame.K_k:
                    kbdmove = (0, -1)
                elif event.key == pygame.K_DOWN or event.key == pygame.K_j:
                    kbdmove = (0, 1)
                elif event.key == pygame.K_LEFT or event.key == pygame.K_h:
                    kbdmove = (-1, 0)
                elif event.key == pygame.K_RIGHT or event.key == pygame.K_l:
                    kbdmove = (1, 0)
                elif event.key == pygame.K_RETURN:
                    jump = True
                elif event.key == pygame.K_ESCAPE:
                    return 1  # not string nor None

                pygame.event.clear()
                break

            elif event.type == pygame.MOUSEBUTTONUP:
                is_mouse_input = True
                if event.button == 1:
                    jump = True
                pygame.event.clear()
                break

        # find the active/highlighted tile following mouse/keyboard navigation:
        if is_mouse_input and time.time() - t1 > 0.01:  # time comparison is so we don't register mouse event when win is first drawn;
            t = get_hovered_tile(pygame.mouse.get_pos(), tiles)
            if t is not None:
                active_tile = t
            else:
                jump = False  # make sure not to change workspace if we clicked between the tiles
        elif kbdmove is not None:
            if kbdmove[0] != 0:  # left-right movement
                if active_tile is None: active_tile = 0
                active_tile += kbdmove[0]
                if active_tile > workspaces - 1:
                    active_tile = 0
                elif active_tile < 0:
                    active_tile = workspaces - 1
            elif len(grid) > 1:  # up-down movement
                if active_tile is None: active_tile = 0
                current_row = tiles[active_tile]['row_idx']
                if current_row == 0:  # we're currently on first row
                    no_of_tiles_on_target_row = grid[kbdmove[1]]
                    prev_tiles = grid[0] if kbdmove[1] == 1 else sum(grid[:len(grid)-1])
                elif current_row == len(grid)-1:  # we're on last row
                    if kbdmove[1] == 1:
                        no_of_tiles_on_target_row = grid[0]
                        prev_tiles = 0
                    else:
                        no_of_tiles_on_target_row = grid[current_row-1]
                        prev_tiles = sum(grid[:len(grid)-2])
                else:
                    no_of_tiles_on_target_row = grid[current_row + kbdmove[1]]
                    prev_tiles = sum(grid[:current_row + kbdmove[1]])

                next_tiles = [i for i in range(prev_tiles, prev_tiles+no_of_tiles_on_target_row)]
                active_tile = get_new_active_tile(tiles, active_tile, next_tiles)

        if jump and active_tile is not None:
            target_ws_num = tiles[active_tile]['ws']
            return 'workspace --no-auto-back-and-forth {}'.format(
                quote_i3_string(GLOBAL_KNOWLEDGE['wss'][target_ws_num]['name'])
            )

        draw_tile_overlays(screen, active_tile, tiles)

        if pygame.display.get_init():  # check as UI might've been closed by on_ws() from other thread
            pygame.display.update()
            pygame.time.wait(50)
    return 1  # not None, not str; unsure about this actually; think this statement is a corner case anyway


def get_new_active_tile(tiles, active_tile, next_tiles):
    def get_center(t):
        return (t['br'][0] + t['ul'][0]) / 2  # given tile's centerpoint x-coord

    i = {}
    at_x = get_center(tiles[active_tile])
    for t in next_tiles:
        j = abs(at_x - get_center(tiles[t]))
        i[j] = t

    return i[min(list(i.keys()))]


# draw/reset highlight overlays:
def draw_tile_overlays(screen, active_tile, tiles):
    # first replace active thumbs with mouseoff/inactive ones, if tile is no longer active:
    for tile in tiles:
        if tile != active_tile and tiles[tile]['active']:
            screen.blit(tiles[tile]['mouseoff'], tiles[tile]['ul'])
            tiles[tile]['active'] = False
    # ...and finally paint mouseon/active thumb for an active/selected tile:
    if active_tile is not None and not tiles[active_tile]['active']:
        screen.blit(tiles[active_tile]['mouseon'], tiles[active_tile]['ul'])
        tiles[active_tile]['active'] = True


def on_ws(i3, e):
    global GLOBAL_UPDATES_RUNNING

    LOGGER.debug(' ---- on ws state: {}, name: {}'.format(e.change, e.current.name))
    tree = i3.get_tree()
    focused_ws = tree.find_focused().workspace()
    gk = update_workspace(e.current, focused_ws)

    if PREVIEW_SWEEP_RUNNING:
        return

    if not GLOBAL_UPDATES_RUNNING:
        # this block gets executed if we exit expo by moving focus to other WS, ie. not by toggling WS change via expo itself

        GLOBAL_UPDATES_RUNNING = True  # make sure UI gets closed on workspace switch (including if we move cursor to neighboring WS when UI is rendered)

        # if a floating window was focused on the WS we just moved away from (that had expo opened),
        # store it in GLOBAL_KNOWLEDGE so we can focus it once we return to that WS.
        # Note this logic is not too great, as e.old.focus[0] seems to bundle the entire
        # tiled container, so it doesn't matter if _actual_ focused window was a floating
        # one or a tiled one, it still resolves to focus[1] and store it if it's a float.
        #
        # as in, if we have floating window visible, focused or not, it'll always be
        # at focus[1] as i3expo window will be listed in container ID-d by focus[0]
        #
        # (note [e.old is not None] implies change='focus')
        old_key = workspace_key(e.old) if e.old is not None else None
        if old_key in GLOBAL_KNOWLEDGE['wss'] and len(e.old.focus) > 1:  # TODO schedule focus events when returning to ws??? sounds like hacky & loads of corner cases
            win = tree.find_by_id(e.old.focus[1])
            if win is not None and win.type == 'floating_con' and win.focus:
                GLOBAL_KNOWLEDGE['wss'][old_key]['ff'] = win.focus[0]
    elif gk['ff'] is not None and e.change == 'focus':
        i3.command('[con_id={}] focus'.format(gk['ff']))
        gk['ff'] = None  # reset

    # TODO: sure we want force=True here?
    WS_UPDATE_DEBOUNCED(i3, e, rate_limit_period=LOOP_INTERVAL, force=True, debounced=True)


def on_ws_empty(i3, e):
    LOGGER.debug(' ---- on ws EMPTY: {}'.format(e.change))

    workspace_names = {workspace.name for workspace in i3.get_tree().workspaces()}
    for name, item in GLOBAL_KNOWLEDGE['wss'].items():
        if name not in workspace_names:
            # Keep the workspace entry so fixed named workspace layouts remain
            # visible and selectable, but do not display windows that were closed.
            item['screenshot'] = []
            item['last-update'] = 0.0
            item['state'] = 0


def on_ws_rename(i3, e):
    LOGGER.debug(' ---- on ws RENAME: {}'.format(e.change))
    old_name = e.old.name if e.old is not None else None
    new_name = e.current.name
    if not old_name or old_name not in GLOBAL_KNOWLEDGE['wss']:
        return

    renamed = {}
    for name, item in GLOBAL_KNOWLEDGE['wss'].items():
        if name == old_name:
            item['name'] = new_name
            item['num'] = e.current.num
            renamed[new_name] = item
        else:
            renamed[name] = item
    GLOBAL_KNOWLEDGE['wss'] = renamed

    if GLOBAL_KNOWLEDGE['active'] == old_name:
        GLOBAL_KNOWLEDGE['active'] = new_name


# note we use the PREVIOUSLY_FOCUSED_WIN hack just because window event doesn't
# include 'old' param such as WorkspaceEvent does
def on_win_focus(i3, e):
    LOGGER.debug(' ---- on win FOCUS: {}'.format(e.change))

    if PREVIEW_SWEEP_RUNNING:
        return

    # TODO: perhaps here we should also check whether the blacklisted window
    #       is still visible in this if-block?
    if GLOBAL_KNOWLEDGE['prev_f_w'] in WIN_CLASS_BLACKLIST:
        UPDATER_DEBOUNCED.reset()
        WS_UPDATE_DEBOUNCED.reset()
        if e.container.window_class != SELF_WIN_CLASS: GLOBAL_KNOWLEDGE['prev_f_w'] = e.container.window_class
        return
    if e.container.window_class != SELF_WIN_CLASS: GLOBAL_KNOWLEDGE['prev_f_w'] = e.container.window_class
    UPDATER_DEBOUNCED(i3, e)


def on_win_title(i3, e):
    LOGGER.debug(' ---- on win TITLE: {}'.format(e.change))
    if PREVIEW_SWEEP_RUNNING:
        return
    if e.container.focused:  # we only care for win title change if it's focused
        UPDATER_DEBOUNCED(i3, e)


SHORTCUT_MODIFIER_MASKS = {
    'shift': 1,
    'control': 4,
    'ctrl': 4,
    'alt': 8,
    'mod1': 8,
    'super': 64,
    'win': 64,
    'mod4': 64,
}


def parse_global_shortcut(shortcut):
    """Parse ``Mod4+e``-style shortcut text into an X11 mask and keysym."""
    shortcut = shortcut.strip()
    if not shortcut:
        return None

    modifiers = 0
    keys = []
    for part in (item.strip() for item in shortcut.split('+')):
        normalized = part.lower()
        if normalized in SHORTCUT_MODIFIER_MASKS:
            modifiers |= SHORTCUT_MODIFIER_MASKS[normalized]
        elif part:
            keys.append(part)

    if len(keys) != 1:
        raise ValueError('shortcut must contain exactly one non-modifier key')

    key = keys[0]
    aliases = {
        'enter': 'Return',
        'return': 'Return',
        'esc': 'Escape',
        'escape': 'Escape',
        'space': 'space',
        'tab': 'Tab',
    }
    key = aliases.get(key.lower(), key)
    if len(key) == 1:
        key = key.lower()
    elif key.lower().startswith('f') and key[1:].isdigit():
        key = key.upper()
    return modifiers, key


def global_shortcut_listener(shortcut):
    """Grab a configurable X11 key and relay it through the normal signal path."""
    try:
        parsed = parse_global_shortcut(shortcut)
        if parsed is None:
            return
        modifiers, key_name = parsed
    except ValueError:
        LOGGER.exception('Invalid global shortcut %s', shortcut)
        return

    try:
        from Xlib import X, XK, display, error
    except ImportError:
        LOGGER.exception('python-xlib is required for global shortcut support')
        return

    try:
        xdisplay = display.Display()
        root = xdisplay.screen().root
        keysym = XK.string_to_keysym(key_name)
        keycode = xdisplay.keysym_to_keycode(keysym) if keysym else 0
        if not keycode:
            raise ValueError('unknown X11 key: {}'.format(key_name))

        # Also work while Caps Lock and the common Num Lock modifier are active.
        ignored_masks = (0, X.LockMask, X.Mod2Mask, X.LockMask | X.Mod2Mask)
        grab_errors = []
        for ignored in ignored_masks:
            catch_error = error.CatchError(error.BadAccess)
            root.grab_key(
                keycode,
                modifiers | ignored,
                False,
                X.GrabModeAsync,
                X.GrabModeAsync,
                onerror=catch_error,
            )
            grab_errors.append(catch_error)
        xdisplay.sync()
        if any(catch_error.get_error() for catch_error in grab_errors):
            xdisplay.close()
            LOGGER.error(
                'Global shortcut %s is already used; change toggle_shortcut in %s',
                shortcut,
                CONFIG_FILE,
            )
            return
        LOGGER.info('Global shortcut enabled: %s', shortcut)

        last_toggle = 0.0
        while True:
            event = xdisplay.next_event()
            if event.type != X.KeyPress or event.detail != keycode:
                continue
            now = time.monotonic()
            if now - last_toggle < 0.35:
                continue
            last_toggle = now
            os.kill(os.getpid(), signal.SIGUSR1)
    except Exception:
        LOGGER.exception('Unable to enable global shortcut %s', shortcut)


def start_global_shortcut_listener(shortcut):
    global SHORTCUT_THREAD

    try:
        parsed = parse_global_shortcut(shortcut)
    except ValueError:
        LOGGER.exception('Invalid global shortcut %s', shortcut)
        return None
    if parsed is None:
        return None
    SHORTCUT_THREAD = Thread(
        target=global_shortcut_listener,
        args=(shortcut,),
        name='i3expo-shortcut',
        daemon=True,
    )
    SHORTCUT_THREAD.start()
    return SHORTCUT_THREAD


def run():
    global i3
    global CONFIG
    global CONFIG_FILE
    global UPDATER_DEBOUNCED
    global WS_UPDATE_DEBOUNCED
    global LOCK

    LOCK = singleton.SingleInstance()
    i3 = i3ipc.Connection()

    converters = {'color': get_color}
    CONFIG = configparser.ConfigParser(converters = converters)
    CONFIG_FILE = os.path.join(xdg_config_home, SELF_WIN_CLASS, 'config')
    hot_reload()  # reads CONFIG and inits other global vars

    init_knowledge()
    UPDATER_DEBOUNCED = Debounce(CONFIG.getfloat('CONF', 'debounce_period_sec'),
                                 partial(update_state, debounced=True))
    WS_UPDATE_DEBOUNCED = Debounce(0.15, update_state)

    signal.signal(signal.SIGINT, signal_quit)
    signal.signal(signal.SIGTERM, signal_quit)
    signal.signal(signal.SIGHUP, signal_reload)
    signal.signal(signal.SIGUSR1, signal_toggle_ui)

    if CONFIG.getboolean('CONF', 'startup_scan'):
        capture_missing_workspace_previews(
            i3,
            shown_ws(),
            CONFIG.getfloat('CONF', 'workspace_capture_delay_sec'),
            force=True,
        )
    update_state(i3, all_active_ws=True, force=True)
    start_global_shortcut_listener(CONFIG.get('CONF', 'toggle_shortcut'))

    # i3.on('window::new', update_state)  # no need when changing on window::focus
    # i3.on('window::close', update_state)  # no need when changing on window::focus
    i3.on('window::move', UPDATER_DEBOUNCED)
    i3.on('window::floating', UPDATER_DEBOUNCED)
    i3.on('window::fullscreen_mode', UPDATER_DEBOUNCED)
    i3.on('window::focus', on_win_focus)
    i3.on('window::title', on_win_title)
    i3.on('workspace::focus', Debounce(0.1, on_ws))  # eg when moving a ws, then many ::focus events seem to be triggered
    i3.on('workspace::init', on_ws)
    i3.on('workspace::move', on_ws)
    i3.on('workspace::restored', on_ws)
    i3.on('workspace::empty', on_ws_empty)
    i3.on('workspace::rename', on_ws_rename)
    i3.on('shutdown', on_shutdown)

    i3_thread = Thread(target = i3.main)
    i3_thread.daemon = True
    i3_thread.start()

    # TODO: consider higher interval for non-focused WS; just to shed some load
    #os.nice(10)  # as per usual, 19 is max; remember decreasing nice is impossible as regular user
    while True:
        time.sleep(LOOP_INTERVAL)
        update_state(i3, rate_limit_period=LOOP_INTERVAL, all_active_ws=True, force=True)


if __name__ == '__main__':  # pragma: no cover
    run()
