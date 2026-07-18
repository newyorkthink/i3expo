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
import pygame
import i3ipc
import copy
import signal
import traceback
import pprint
import time
import math
import logging
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

qm_cache = {}  # screen_w x screen_h mapped against rendered question mark for missing tiles
LOCK = singleton.SingleInstance()
LOGGER = logging.getLogger(__name__)


# def _runtime_path() -> str:
    # return  xdg.BaseDirectory.get_runtime_dir()

def _runtime_path() -> str:
    xdg_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    dir_path = xdg_dir + "/i3expo"
    if not os.path.isdir(dir_path):
        os.makedirs(dir_path)
    return dir_path

RUNTIME_PATH = _runtime_path()

def shutdown_common():
    global GLOBAL_UPDATES_RUNNING

    LOGGER.info('Shutting down...')

    try:
        GLOBAL_UPDATES_RUNNING = False
        UPDATER_DEBOUNCED.reset()
        WS_UPDATE_DEBOUNCED.reset()
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
    default = {'active': -1,  # 'active' = currently active ws num
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
                return s.get('gknowledge', default)
    except Exception as e:
        LOGGER.error(e)
    return default


def persist_state():
    global GLOBAL_UPDATES_RUNNING

    GLOBAL_UPDATES_RUNNING = False

    try:
        # pp.pprint(GLOBAL_KNOWLEDGE)
        for k, v in GLOBAL_KNOWLEDGE['wss'].items():
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
    if e.change == 'restart' and CONFIG.getboolean('CONF', 'store_state_on_restart'):
        persist_state()
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
        # i3.command('workspace i3expo-temporary-workspace')  # jump to temp ws; doesn't seem to work well in multimon setup; introduced by  https://gitlab.com/d.reis/i3expo/-/commit/d14685d16fd140b3a7374887ca086ea66e0388f5 - looks like it solves problem where fullscreen state is lost on expo toggle
        GLOBAL_UPDATES_RUNNING = False
        UPDATER_DEBOUNCED.reset()
        WS_UPDATE_DEBOUNCED.reset()

        # ui_thread = Thread(target = show_ui)
        # ui_thread.daemon = True
        try:
            show_ui(wss)
        except Exception as e:
            # LOGGER.error(e)
            pass


def get_color(raw):
    return pygame.Color(raw)


def read_config():
    CONFIG.read_dict({
        'CONF': {
            'bgcolor'                    : 'gray20',
            'frame_active_color'         : '#5a6da4',
            'frame_inactive_color'       : '#93afb3',
            'frame_missing_color'        : '#ffe6d0',
            'tile_missing_color'         : 'gray40',

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
            'names_font'                 : 'verdana',  # list with pygame.font.get_fonts()
            'names_fontsize'             : 25,
            'names_color'                : 'white',
            'highlight_percentage'       : 20,
            'screenshot_lib_path'        : os.path.join(os.path.dirname(os.path.realpath(__file__)), 'prtscn.so'),
            'store_state_on_restart'     : True,
            'max_persisted_state_age_sec': 2,
            'state_f'                    : f'{RUNTIME_PATH}/{SELF_WIN_CLASS}.state',
            'log_lvl'                    : 'INFO'
        }
    })

    if os.path.exists(CONFIG_FILE):
        CONFIG.read(CONFIG_FILE)
    # else:
        # # write config file down if not existing:
        # root_dir = os.path.dirname(CONFIG_FILE)
        # if not os.path.exists(root_dir):
            # os.makedirs(root_dir)
        # with open(CONFIG_FILE, 'w') as f:
            # CONFIG.write(f)


def grab_screen(i):
    # LOGGER.debug('GRABBING FOR: {}'.format(i['name']))
    w = i['w']
    h = i['h']

    result = (ctypes.c_ubyte * w * h * 3)()  # *3 for R,G,B
    GRAB.getScreen(i['x'], i['y'], w, h, result)
    return [w, h, result]


def update_workspace(ws, focused_ws, hydration=False) -> dict:
    i = GLOBAL_KNOWLEDGE['wss'].get(ws.num)
    if i is None:
        i = GLOBAL_KNOWLEDGE['wss'][ws.num] = {
            'op'          : ws.ipc_data['output'],
            'name'        : ws.name,
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

    # some data should not be set on first state hydration, e.g. missing polybar
    # on initial restart causes different w & h values than our loaded data,
    # causing error on render
    if hydration:
        i['id'] = ws.id
        i['name'] = ws.name
        #i['op'] = ws.ipc_data['output']
    else:
        # always update dimensions; eg ws might've been moved onto a different output:
        i['x'] = ws.rect.x
        i['y'] = ws.rect.y
        i['w'] = ws.rect.width
        i['h'] = ws.rect.height
        i['ratio'] = ws.rect.width / ws.rect.height

    if ws.id == focused_ws.id:
        # LOGGER.debug('active WS:: {}'.format(ws.name))
        GLOBAL_KNOWLEDGE['active'] = ws.num
    return i


def init_knowledge():
    global GLOBAL_KNOWLEDGE

    GLOBAL_KNOWLEDGE = load_global_knowledge()
    state_hydration = GLOBAL_KNOWLEDGE['active'] != -1

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
    if rate_limit_period is not None and t - wk['last-update'] <= rate_limit_period:
        return False
    return update_tree_state(ws, wk) or force


def update_state(i3, e=None, rate_limit_period=None,
                 force=False, debounced=False,
                 all_active_ws=False):
    LOGGER.debug('[ TOGGLING updat_state(){}; force: {}, debounced: {}'.format(' by event [' + e.change + ']' if e else '', force, debounced))

    time.sleep(0.2)  # TODO system-specific; configurize? also, maybe only sleep if it's _not_ debounced?

    tree = i3.get_tree()
    focused_con = tree.find_focused()

    if (not GLOBAL_UPDATES_RUNNING or
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
            LOGGER.debug('  -> grabbing WS {} image took {}'.format(ws.num, time.time()-t1))
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

    screen = pygame.display.set_mode((ws['w'], ws['h']), pygame.RESIZABLE)
    pygame.display.set_caption(SELF_WIN_CLASS)

    tiles = {}  # contains grid tile index to thumbnail/ws_screenshot data mappings
    active_tile = None

    wss.sort()
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
                'ws'        : ws_num,        # workspace.num this tile represents;
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


def draw_missing_tile(screen_w, screen_h):
    key = '{}x{}'.format(screen_w, screen_h)

    if key in qm_cache:
        return qm_cache[key]

    missing_tile = pygame.Surface((screen_w, screen_h), pygame.SRCALPHA, 32)
    missing_tile = missing_tile.convert_alpha()
    qm = pygame.font.SysFont('sans-serif', screen_h).render('?', True, (150, 150, 150))
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
    font = pygame.font.SysFont(names_font, names_fontsize)

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
            return 'workspace ' + str(GLOBAL_KNOWLEDGE['wss'][target_ws_num]['name'])

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

    LOGGER.debug(' ---- on ws state: {}, num: {}'.format(e.change, e.current.num))
    gk = GLOBAL_KNOWLEDGE['wss']
    if e.current.num in gk:
        gk = gk[e.current.num]
        gk['op'] = e.current.ipc_data['output']
    else:
        gk = None

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
        if e.old is not None and e.old.num in GLOBAL_KNOWLEDGE['wss'] and len(e.old.focus) > 1:  # TODO schedule focus events when returning to ws??? sounds like hacky & loads of corner cases
            win = i3.get_tree().find_by_id(e.old.focus[1])
            if win.type == 'floating_con' and win.focus:
                GLOBAL_KNOWLEDGE['wss'][e.old.num]['ff'] = win.focus[0]
    elif gk is not None and gk['ff'] is not None and e.change == 'focus':
        i3.command('[con_id={}] focus'.format(gk['ff']))
        gk['ff'] = None  # reset

    # TODO: sure we want force=True here?
    WS_UPDATE_DEBOUNCED(i3, e, rate_limit_period=LOOP_INTERVAL, force=True, debounced=True)


def on_ws_empty(i3, e):
    LOGGER.debug(' ---- on ws EMPTY: {}'.format(e.change))

    wspace_nums = [w.num for w in i3.get_tree().workspaces()]
    deleted = [n for n in GLOBAL_KNOWLEDGE['wss'] if n not in wspace_nums]
    for n in deleted:
        del GLOBAL_KNOWLEDGE['wss'][n]


def on_ws_rename(i3, e):
    LOGGER.debug(' ---- on ws RENAME: {}'.format(e.change))
    gk = GLOBAL_KNOWLEDGE['wss']
    if e.current.num in gk:
        gk[e.current.num]['name'] = e.current.name


# note we use the PREVIOUSLY_FOCUSED_WIN hack just because window event doesn't
# include 'old' param such as WorkspaceEvent does
def on_win_focus(i3, e):
    LOGGER.debug(' ---- on win FOCUS: {}'.format(e.change))

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
    if e.container.focused:  # we only care for win title change if it's focused
        UPDATER_DEBOUNCED(i3, e)


def run():
    global i3
    global CONFIG
    global CONFIG_FILE
    global UPDATER_DEBOUNCED
    global WS_UPDATE_DEBOUNCED

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

    update_state(i3, all_active_ws=True)

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
