import asyncio
import heapq
import json
import random
import time
import websockets

from websockets.client import WebSocketClientProtocol


SIZE = 15
SIZE2 = SIZE * SIZE
UNREACHABLE = 10000000


class Cell:
    def __init__(self, board, position):
        self.board = board
        self.x = position % SIZE
        self.y = position // SIZE
        self.west, self.north, self.east, self.south = None, None, None, None
        self.dists = {} # {unit_id: (dist, prev_cell)}
        self.safe_dists = {} # {unit_id: (dist, prev_cell)}
        self.safe_paths = None
        self.target_range = [0] * 10 # number of targets within range 1, 2, 3, 4...
        self.unit = None
        self.hp = 0
        self.wall = False # Only true for indestructible walls
        self.box = False # Only true for destructible wooden/ore blocks
        self.created = None
        self.expires = None
        self.bomb_diameter = None # int: blast diameter of bomb
        self.bomb_unit = None
        self.fire = None # bool
        self.blast_powerup = None
        self.freeze_powerup = None
        self.future_fire_start = {} # tick that fire may start
        self.future_fire_end = {} # tick that fire may last until
        self.eog_fire = False

    def _on_entity_expired(self):
        if self.bomb_unit:
            self.bomb_unit.bombs.remove(self)
            self.bomb_unit.player.bombs.remove(self)
        self.hp = 0
        self.wall = False
        self.box = False
        self.created = None
        self.expires = None
        self.bomb_diameter = None
        self.bomb_unit = None
        self.fire = None
        self.blast_powerup = None
        self.freeze_powerup = None
        self.future_fire_start = {}
        self.future_fire_end = {}

    def neighbor(self, dx, dy):
        x, y = self.x + dx, self.y + dy
        if 0 <= x < SIZE and 0 <= y < SIZE:
            return self.board.cell(x, y)
        return None

    def search_neighbors(self, player):
        cells = random.sample([self.north, self.east, self.south, self.west], 4)
        return [cell for cell in cells
                if cell # cell exists
                and not cell.wall # cell is not impenetrable
                # TODO opp only impenetrable if same pos and move for 2-3+ turns
                and not (cell.unit and (cell.unit.hp <= 0 or not cell.unit.player is player))] # No dead/opp unit

    def move_neighbors(self):
        cells = random.sample([self.north, self.east, self.south, self.west, self], 5)
        return [cell for cell in cells
                if cell # cell exists
                and not cell.wall and not cell.bomb_diameter and not cell.box  # No blocking entity
                and not (cell.unit and cell.unit.hp <= 0)] # No dead unit

    def safe_turns(self, player, invulnerable):
        own_id, opp_id = player.id, ('a' if player.id == 'b' else 'b')

        danger_ranges = []
        if self.fire:
            danger_ranges.append((self.created, self.expires))
        if self.future_fire_start:
            # fire_start = 5
            #   tick = 2, arrive = 3 (Ok, can leave on 4 safely)
            #   tick = 3, arrive = 4 (Bad, can't leave in time)
            #   tick = 4, arrive = 5 (Bad, arrive on fire turn)
            # fire_end = 10
            #   tick = 8, arrive = 9 (Bad)
            #   tick = 9, arrive = 10 (Ok)
            #   tick = 10, arrive = 11 (Ok)
            if opp_id in self.future_fire_start:
                danger_ranges.append((self.future_fire_start[opp_id], self.future_fire_end[opp_id]))
            if own_id in self.future_fire_end:
                danger_ranges.append((self.future_fire_end[own_id] - 5, self.future_fire_end[own_id]))

        min_danger_start = UNREACHABLE
        for danger_start, safe_begin in danger_ranges:
            danger_start = max(danger_start, invulnerable + 1)
            if danger_start <= self.board.tick + 1 < safe_begin:
                return 0
            if self.board.tick + 1 < safe_begin and danger_start < min_danger_start:
                min_danger_start = danger_start
        return min_danger_start - self.board.tick - 1

    def is_safe(self, player, arrival_tick, invulnerable):
        '''Returns (bool, int) for is_safe, additional ticks necessary to wait'''
        own_id, opp_id = player.id, ('a' if player.id == 'b' else 'b')

        # If inaccessible
        if (self.wall
            or self.box
            or self.bomb_diameter
            or (self.unit and self.unit.hp <= 0) # dead
            # TODO: instead check for any unit with consistent pos/move
            or self.unit and not self.unit.player is player): # opp unit
            return False, 0

        # Determine danger range for current/future fire
        danger_ranges = []
        if self.fire:
            danger_ranges.append((self.created, self.expires))
        if self.future_fire_start:
            # fire_start = 5
            #   tick = 2, arrive = 3 (Ok, can leave on 4 safely)
            #   tick = 3, arrive = 4 (Bad, can't leave in time)
            #   tick = 4, arrive = 5 (Bad, arrive on fire turn)
            # fire_end = 10
            #   tick = 8, arrive = 9 (Bad)
            #   tick = 9, arrive = 10 (Ok)
            #   tick = 10, arrive = 11 (Ok)
            if opp_id in self.future_fire_start:
                danger_ranges.append((self.future_fire_start[opp_id], self.future_fire_end[opp_id]))
            if own_id in self.future_fire_end:
                danger_ranges.append((self.future_fire_end[own_id] - 5, self.future_fire_end[own_id]))

        safe_wait = 0
        safe_wait_adjusted = True
        while safe_wait_adjusted:
            safe_wait_adjusted = False
            for danger_start, safe_begin in danger_ranges:
                # Vulnerable and need to wait 1 turn
                if (safe_wait + arrival_tick + 1 == safe_begin  # only need to survive 1 tick (arrival tick)
                    and invulnerable < safe_wait + arrival_tick):  # vulnerable upon arrival (or earlier)
                    assert safe_begin - arrival_tick > safe_wait  # should only increase
                    safe_wait = safe_begin - arrival_tick  # Wait until fire expires (1 turn)
                    safe_wait_adjusted = True
                    #print(f'(A) Safe wait {safe_wait} ticks before ({self.x},{self.y}): arrival={arrival_tick}, range={danger_start}..{safe_begin}')
                    break

                # Vulnerable and need to wait 2+ turns
                if (danger_start <= safe_wait + arrival_tick + 1 < safe_begin  # Need 2+ ticks at dest (arrive tick and depart tick)
                    and invulnerable < safe_wait + arrival_tick + 1):  # Vulnerable the turn after arrival (or earlier)
                    assert safe_begin - arrival_tick > safe_wait
                    safe_wait = safe_begin - arrival_tick  # Wait until fire expires
                    safe_wait_adjusted = True
                    #print(f'(B) Safe wait {safe_wait} ticks before ({self.x},{self.y}): arrival={arrival_tick}, range={danger_start}..{safe_begin}')
                    break

        # Safe given a certain waiting period (can be 0)
        return True, safe_wait

    def get_safe_paths(self, player, invulnerable, ok_cells=None, bad_cells=None):
        '''Returns list of safe dest lists for 1-6tick (longer than invuln and bomb priming) cells'''
        temp_id = 'temp'
        for cell in self.board.cells:
            cell.safe_dists[temp_id] = (UNREACHABLE, None)

        safe_at_dist = [[] for _ in range(6+1)]
        self.safe_dists[temp_id] = (0, None)
        queue = [(0, 0, self)]
        while queue:
            dist, _, cell = heapq.heappop(queue)
            if dist >= len(safe_at_dist):
                continue

            safe, safe_wait = True, 0
            if dist > 0:  # Intrinsically "safe" initially in the sense that we can't change the present
                safe, safe_wait = cell.is_safe(player, self.board.tick + dist, invulnerable)
            if not safe:
                continue

            if dist + safe_wait >= len(safe_at_dist):
                continue

            wait_is_ok = True
            for i in range(safe_wait):
                # Confirm that this new timing can work with the preceding cell in the path
                # We know that prev_cell is ok at tick + dist - 1
                #               and cell is ok at tick + dist + safe_wait
                # Need to confirm tick + dist through tick + dist + safe_wait - 1
                prev_cell = cell.safe_dists[temp_id][1]
                prev_safe, prev_safe_wait = prev_cell.is_safe(player, self.board.tick + dist + i, invulnerable)
                if (not prev_safe and not prev_cell is self) or prev_safe_wait != 0:
                    wait_is_ok = False
                    break
            if not wait_is_ok:
                continue

            safe_at_dist[dist + safe_wait].append(cell)

            for new_cell in cell.search_neighbors(player):                          
                new_dist = dist + safe_wait + 1
                if new_dist < new_cell.safe_dists[temp_id][0]:
                    new_cell.safe_dists[temp_id] = (new_dist, cell)
                    heapq.heappush(queue, (new_dist, random.random(), new_cell))

        safe_paths = safe_at_dist
        print(f'unit {self.unit.id} safe paths:  ({invulnerable} {self.unit.invulnerable} {self.unit.stunned})')
        for i in range(len(safe_paths)):
            s = f'{i} ({len(safe_paths[i])}): '
            safe_paths[i].sort(key = lambda x: 100 * x.y + x.x)
            for c in safe_paths[i]:
                s = s + f'({c.x},{c.y}), '
            print(s)
                    
        return safe_at_dist

    def get_safe_dist(self, other_cell, player, invulnerable, stunned):
        opp_id = 'a' if player.id == 'b' else 'b'
        temp_id = 'temp'
        for cell in self.board.cells:
            cell.safe_dists[temp_id] = (UNREACHABLE, None)

        init_dist = max(0, stunned - self.board.tick)
        self.safe_dists[temp_id] = (0, None)
        queue = [(init_dist, 0, self)]
        while queue:
            dist, _, cell = heapq.heappop(queue)

            safe, safe_wait = True, 0
            if dist > init_dist:  # Intrinsically "safe" initially in the sense that we can't change the present
                safe, safe_wait = cell.is_safe(player, self.board.tick + dist, invulnerable)
            if not safe:
                continue

            wait_is_ok = True
            for i in range(safe_wait):
                # Confirm that this new timing can work with the preceding cell in the path
                # We know that prev_cell is ok at tick + dist - 1
                #               and cell is ok at tick + dist + safe_wait
                # Need to confirm tick + dist through tick + dist + safe_wait - 1
                prev_cell = cell.safe_dists[temp_id][1]
                prev_safe, prev_safe_wait = prev_cell.is_safe(player, self.board.tick + dist + i, invulnerable)
                if (not prev_safe and not prev_cell is self) or prev_safe_wait != 0:
                    wait_is_ok = False
                    break
            if not wait_is_ok:
                continue

            if cell is other_cell:
                return dist # todo also include initial move cell?

            for new_cell in cell.search_neighbors(player):
                new_dist = dist + safe_wait + 1
                if new_dist < new_cell.safe_dists[temp_id][0]:
                    new_cell.safe_dists[temp_id] = (new_dist, cell)
                    heapq.heappush(queue, (new_dist, random.random(), new_cell))
        return UNREACHABLE

    def get_dist(self, other_cell, player):
        opp_id = 'a' if player.id == 'b' else 'b'
        temp_id = 'temp'
        for cell in self.board.cells:
            cell.dists[temp_id] = (UNREACHABLE, None)

        self.dists[temp_id] = (0, None)
        queue = [(0, 0, self)]
        while queue:
            dist, _, cell = heapq.heappop(queue)
            if cell is other_cell:
                return dist
            for new_cell in cell.search_neighbors(player):
                new_dist = dist + 1
                if new_cell.box:
                    new_dist += 14 * new_cell.hp

                arrival_tick = self.board.tick + new_dist
                if (opp_id in new_cell.future_fire_end
                    and new_cell.future_fire_start[opp_id] <= arrival_tick + 1 <= new_cell.future_fire_end[opp_id]):
                    new_dist = new_cell.future_fire_end[opp_id] - self.board.tick

                if new_dist < new_cell.dists[temp_id][0]:
                    new_cell.dists[temp_id] = (new_dist, cell)
                    heapq.heappush(queue, (new_dist, random.random(), new_cell))
        return UNREACHABLE

    def _update_safe_paths(self, unit_id, player):
        assert self.unit and self.unit.id == unit_id
        self.safe_paths = self.get_safe_paths(player, self.unit.invulnerable)

    def _update_safe_dists_to_all(self, unit_id, player):
        assert self.unit and self.unit.id == unit_id

        for cell in self.board.cells:
            cell.safe_dists[unit_id] = (UNREACHABLE, None)

        if self.unit.hp <= 0:
            return

        init_dist = max(0, self.unit.stunned - self.board.tick)
        opp_id = 'a' if player.id == 'b' else 'b'
        self.safe_dists[unit_id] = (0, None)
        queue = [(init_dist, 0, self)]
        while queue:
            dist, _, cell = heapq.heappop(queue)

            safe, safe_wait = True, 0
            if dist > init_dist:  # Intrinsically "safe" initially in the sense that we can't change the present
                safe, safe_wait = cell.is_safe(player, self.board.tick + dist, self.unit.invulnerable)
            if not safe:
                continue

            wait_is_ok = True
            for i in range(safe_wait):
                # Confirm that this new timing can work with the preceding cell in the path
                # We know that prev_cell is ok at tick + dist - 1
                #               and cell is ok at tick + dist + safe_wait
                # Need to confirm tick + dist through tick + dist + safe_wait - 1
                prev_cell = cell.safe_dists[unit_id][1]
                prev_safe, prev_safe_wait = prev_cell.is_safe(player, self.board.tick + dist + i, self.unit.invulnerable)
                if (not prev_safe and not prev_cell is self) or prev_safe_wait != 0:
                    wait_is_ok = False
                    break
            if not wait_is_ok:
                continue

            for new_cell in cell.search_neighbors(player):
                new_dist = dist + safe_wait + 1
                if new_dist < new_cell.safe_dists[unit_id][0]:
                    new_cell.safe_dists[unit_id] = (new_dist, cell)
                    heapq.heappush(queue, (new_dist, random.random(), new_cell))
    
    def _update_dists_to_all(self, unit_id, player):
        assert self.unit and self.unit.id == unit_id

        for cell in self.board.cells:
            cell.dists[unit_id] = (UNREACHABLE, None)

        opp_id = 'a' if player.id == 'b' else 'b'
        self.dists[unit_id] = (0, None)
        queue = [(0, 0, self)]
        while queue:
            dist, _, cell = heapq.heappop(queue)
            for new_cell in cell.search_neighbors(player):
                new_dist = dist + 1
                if new_cell.box:
                    new_dist += 14 * new_cell.hp

                arrival_tick = self.board.tick + new_dist
                if (opp_id in new_cell.future_fire_end
                    and new_cell.future_fire_start[opp_id] <= arrival_tick < new_cell.future_fire_end[opp_id]):
                    new_dist = new_cell.future_fire_end[opp_id] - self.board.tick

                if new_dist < new_cell.dists[unit_id][0]:
                    new_cell.dists[unit_id] = (new_dist, cell)
                    heapq.heappush(queue, (new_dist, random.random(), new_cell))
        #print(f'Dists for unit {self.id} at {self.x},{self.y}')
        #for y in range(SIZE - 1, -1, -1):
        #    s = ''
        #    for cell in self.board.cells[(y * SIZE):(y * SIZE + SIZE)]:
        #        if cell.dists[self.id][0] == UNREACHABLE:
        #            s += '--\t'
        #        else:
        #            s += str(cell.dists[self.id][0]) + '\t'
        #    print(s)

    def _init_neighbors(self):
        self.west = self.neighbor(-1, 0)
        self.north = self.neighbor(0, 1)
        self.east = self.neighbor(1, 0)
        self.south = self.neighbor(0, -1)

    def _on_entity_spawned(self, payload):
        etype = payload['type']
        self.created = payload['created']
        self.expires = payload.get('expires')
        self.hp = payload.get('hp')
        if etype == 'b':
            self.bomb_diameter = payload['blast_diameter']
            self.bomb_unit = self.board.units[payload['unit_id']]
            assert not self in self.bomb_unit.bombs
            assert not self in self.bomb_unit.player.bombs
            self.bomb_unit.bombs.append(self)
            self.bomb_unit.player.bombs.append(self)
            assert len(self.bomb_unit.bombs) <= 3
            assert len(self.bomb_unit.player.bombs) <= 3
        elif etype == 'x':
            self.fire = True
        elif etype == 'bp':
            self.blast_powerup = True
        elif etype == 'fp':
            self.freeze_powerup = True
        elif etype == 'm':
            self.wall = True
        elif etype == 'w' or etype == 'o':
            self.box = True
        if self.fire and self.expires is None: # end-of-game fire
            self.expires = 2000
            self.eog_fire = True


class Unit:
    def __init__(self, board, id):
        self.board = board
        self.id = id
        self.x, self.y = None, None
        self.cell = None
        self.player = None
        self.hp = 3
        self.diameter = 3
        self.invulnerable = 0 # int: lasts until this tick
        self.stunned = 0 # int: lasts until this tick
        self.goal_list = []
        self.goal_cell = None
        self.bombs = []

    def set_goal_list(self):
        '''Return list of three (cell, score) pairs in order'''
        goal_list = []
        opp_id = 'a' if self.player.id == 'b' else 'b'

        for cell in self.board.cells:
            goal_score = 0
            target_range_i = min(len(cell.target_range) - 1, ((self.diameter // 2) - 1))
            target_range = cell.target_range[target_range_i]
            target_range = target_range if (self.player.id == 'a') else -target_range
            cell_dist =  cell.dists[self.id][0]

            # If bomb placed and in future fire: find safety.
            if self.bombs and self.cell.future_fire_start:
                # TODO better safety measurment using convolution
                goal_score = 100
                if cell.wall or cell.box or cell.bomb_diameter:
                    goal_score -= 100
                if self.player.id in cell.future_fire_start:
                    goal_score -= 10
                if opp_id in cell.future_fire_start:
                    goal_score -= 25
                if cell.unit:
                    if cell.unit.player is self.player:
                        goal_score -= 3
                    else:
                        goal_score -= 5
                if cell.freeze_powerup:
                    goal_score += 5
                if cell.blast_powerup:
                    goal_score += 3
                goal_score -= cell_dist
            else:  # Otherwise find something to do.
                cell_score = 0
                if cell.blast_powerup:
                    cell_score += 10
                if cell.freeze_powerup:
                    cell_score += 15
                if opp_id in cell.future_fire_start: # TODO own or opp?
                    cell_score -= 15
                if cell.wall or cell.box or cell.bomb_diameter:
                    cell_score -= 20
                cell_score += target_range
                goal_score = cell_score * (0.9 ** cell_dist)
            goal_list.append((goal_score, cell))

        goal_list.sort(key=lambda x: x[0], reverse=True)
        self.goal_list = goal_list[:3]

    def _update_dists(self):
        self.cell._update_dists_to_all(self.id, self.player)
        self.cell._update_safe_dists_to_all(self.id, self.player)
        self.cell._update_safe_paths(self.id, self.player)

    def _on_unit_state(self, payload):
        if self.cell and self.cell.unit == self:
            self.cell.unit = None
        self.x, self.y = payload['coordinates']
        self.cell = self.board.cells[self.y * SIZE + self.x]
        self.cell.unit = self
        self.player = self.board.players['a'] if payload['agent_id'] == 'a' else self.board.players['b']
        self.hp = payload['hp']
        self.diameter = payload['blast_diameter']
        self.invulnerable = payload['invulnerable']
        self.stunned = payload['stunned']

    def _on_unit_move(self, move_action):
        if self.cell.unit == self:
            self.cell.unit = None
        if move_action == "up":
            self.y += 1
        elif move_action == "down":
            self.y -= 1
        elif move_action == "right":
            self.x += 1
        elif move_action == "left":
            self.x -= 1
        self.cell = self.board.cells[self.y * SIZE + self.x]
        self.cell.unit = self


class Player:
    def __init__(self, id):
        self.id = id
        self.units = []
        self.bombs = []


class Board:
    def __init__(self, game_state):
        self.tick = 0

        self.cells = [Cell(self, i) for i in range(SIZE2)]
        self.players = {player_id: Player(player_id) for player_id in ['a','b']}
        self.units = {unit_id: Unit(self, unit_id) for unit_id in ['c','d','e','f','g','h']}

        for unit_id in game_state['unit_state']:
            self.units[unit_id]._on_unit_state(game_state['unit_state'][unit_id])
        for unit_id in game_state['agents']['a']['unit_ids']:
            self.players['a'].units.append(self.units[unit_id])
        for unit_id in game_state['agents']['b']['unit_ids']:
            self.players['b'].units.append(self.units[unit_id])

        for cell in self.cells:
            cell._init_neighbors()
        for entity in game_state['entities']:
            self._on_entity_spawned(entity)

        agent_id = game_state['connection']['agent_id']
        self.player = self.players['a'] if agent_id == 'a' else self.players['b']
        self.opp = self.players['a'] if agent_id == 'b' else self.players['b']

    def cell(self, x, y):
        return self.cells[y * SIZE + x]

    def get_bomb_area(self, cell, diameter=None):
        bomb_cells = set()
        blast_cells = set()
        new_bomb_cells = [cell]
        while True:
            if not new_bomb_cells:
                break
            bomb_cell = new_bomb_cells.pop()
            if bomb_cell in bomb_cells:
                continue
            bomb_cells.add(bomb_cell)
            blast_cells.add(bomb_cell)
            for direction in ('north', 'south', 'east', 'west'):
                nearby_cell = bomb_cell
                bomb_diameter = diameter if (not diameter is None and bomb_cell is cell) else bomb_cell.bomb_diameter
                for _ in range(bomb_diameter // 2):
                    nearby_cell = getattr(nearby_cell, direction)
                    if not nearby_cell or nearby_cell.wall or nearby_cell.blast_powerup or nearby_cell.freeze_powerup:
                        break
                    blast_cells.add(nearby_cell)
                    if nearby_cell.box:
                        break
                    if nearby_cell.bomb_diameter:
                        new_bomb_cells.append(nearby_cell)
        return list(blast_cells)

    def _update_dists(self):
        for cell in self.cells:
            cell.safe_paths = None
        for unit in self.units.values():
            unit._update_dists()

    def _update_target_range(self):
        '''Positive if it favors player a, negative for player b'''
        for cell in self.cells:
            cell.target_range = [0] * len(cell.target_range)
        for cell in self.cells:
            if cell.wall or cell.box:
                continue
            for direction in ('north', 'south', 'east', 'west'):
                nearby_cell = cell
                for dist in range(len(cell.target_range)):
                    nearby_cell = getattr(nearby_cell, direction)
                    if not nearby_cell or nearby_cell.wall or nearby_cell.blast_powerup or nearby_cell.freeze_powerup:
                        break
                    if nearby_cell.box:
                        min_dist = { 'a': UNREACHABLE, 'b': UNREACHABLE }
                        for unit_id, unit in self.units.items():
                            if nearby_cell.safe_dists[unit_id][0] < min_dist[unit.player.id]: # todo safe dist to boxes?
                                min_dist[unit.player.id] = nearby_cell.safe_dists[unit_id][0]
                        multiplier = 0
                        if min_dist['a'] < min_dist['b']:
                            multiplier = 1
                        elif min_dist['a'] > min_dist['b']:
                            multiplier = -1
                        for i in range(dist, len(cell.target_range)):
                            cell.target_range[i] += multiplier / (10 ** (nearby_cell.hp - 1)) # 1, 0.1, 0.01
                        break
                    if (nearby_cell.unit
                        and nearby_cell.unit.hp > 0 # not dead
                        and nearby_cell.unit.stunned >= self.tick + 1 + 5): # still stunned when bomb can go off
                        multiplier = 1 if (nearby_cell.unit.player.id == 'b') else -1
                        for i in range(dist, len(cell.target_range)):
                            cell.target_range[i] += multiplier * 20
        #print(f'Target range values:')
        #for y in range(SIZE - 1, -1, -1):
        #    s = ''
        #    for cell in self.cells[(y * SIZE):(y * SIZE + SIZE)]:
        #        s += str((cell.target_range[0], cell.target_range[1])) + '\t'
        #    print(s)

    def _on_bomb_placed(self, cell, start=None, end=None, player_id=None, bombs_processed=None):
        '''Can be called more than once, and on different ticks'''
        if start is None and end is None:
            start, end = cell.created + 5, cell.expires + 5
        if player_id is None:
            player_id = cell.bomb_unit.player.id
        if bombs_processed is None:
            bombs_processed = [cell]
        radius = (cell.bomb_diameter // 2) + 1

        def _set_future_fire(cell, count, direction, player_id, bombs_processed):
            if cell is None or count == 0 or cell.box or cell.wall:
                return
            if player_id in cell.future_fire_start: # Take the conservative start/end if overlapping
                cell.future_fire_start[player_id] = min(cell.future_fire_start[player_id], start)
                cell.future_fire_end[player_id] = min(cell.future_fire_end[player_id], end)
            else:
                cell.future_fire_start[player_id], cell.future_fire_end[player_id] = start, end
            _set_future_fire(getattr(cell, direction), count - 1, direction, player_id, bombs_processed)

            if cell.bomb_diameter and cell not in bombs_processed:
                bombs_processed.append(cell)
                self._on_bomb_placed(cell, start=start, end=end, player_id=player_id, bombs_processed=bombs_processed)

        for direction in ('north', 'south', 'east', 'west'):
            _set_future_fire(cell, radius, direction, player_id, bombs_processed)

    def _on_entity_spawned(self, payload):
        x, y = payload['x'], payload['y']
        self.cells[y * SIZE + x]._on_entity_spawned(payload)

    def _on_entity_expired(self, x, y):
        self.cells[y * SIZE + x]._on_entity_expired()

    def _on_unit_state(self, payload):
        unit_id = payload['unit_id']
        self.units[unit_id]._on_unit_state(payload)


class GameState:
    def __init__(self, connection_string: str):
        self._connection_string = connection_string
        self.board = None
        self._tick_callback = None

    def set_game_tick_callback(self, generate_agent_action_callback):
        self._tick_callback = generate_agent_action_callback

    async def connect(self):
        self.connection = await websockets.connect(self._connection_string)
        if self.connection.open:
            return self.connection

    async def _send(self, packet):
        await self.connection.send(json.dumps(packet))

    async def send_move(self, move: str, unit_id: str):
        packet = {"type": "move", "move": move, "unit_id": unit_id}
        await self._send(packet)

    async def send_bomb(self, unit_id: str):
        packet = {"type": "bomb", "unit_id": unit_id}
        await self._send(packet)

    async def send_detonate(self, x, y, unit_id: str):
        packet = {"type": "detonate", "coordinates": [
            x, y], "unit_id": unit_id}
        await self._send(packet)

    async def _handle_messages(self, connection: WebSocketClientProtocol):
        while True:
            try:
                raw_data = await connection.recv()
                data = json.loads(raw_data)
                await self._on_data(data)
            except websockets.exceptions.ConnectionClosed:
                print('Connection with server closed')
                break

    async def _on_data(self, data):
        data_type = data.get("type")

        if data_type == "info":
            pass # no-op
        elif data_type == "game_state":
            payload = data.get("payload")
            self._on_game_state(payload)
        elif data_type == "tick":
            payload = data.get("payload")
            await self._on_game_tick(payload)
        elif data_type == "endgame_state":
            payload = data.get("payload")
            winning_agent_id = payload.get("winning_agent_id")
            print(f"Game over. Winner: Agent {winning_agent_id}")
        else:
            print(f"unknown packet \"{data_type}\": {data}")

    def _on_game_state(self, game_state):
        '''Recevie initial game state'''
        self.board = Board(game_state)
        self.board._client = self

    async def _on_game_tick(self, game_tick):
        tick_start_time = time.time()
        events = game_tick.get("events")
        for event in events:
            event_type = event.get("type")
            if event_type == "entity_spawned":
                spawn_payload = event.get("data")
                self.board._on_entity_spawned(spawn_payload)
            elif event_type == "entity_expired":
                x, y = event.get('data')
                self.board._on_entity_expired(x, y)
            elif event_type == "unit_state":
                payload = event.get("data")
                self.board._on_unit_state(payload)
            elif event_type == "entity_state":
                x, y = event.get("coordinates")
                updated_entity = event.get("updated_entity")
                self.board._on_entity_expired(x, y)
                self.board._on_entity_spawned(updated_entity)
            elif event_type == "unit":
                unit_action = event.get("data")
                self._on_unit_action(unit_action)
            else:
                print(f"unknown event type {event_type}: {event}")

        # Clear future fire tick values
        for cell in self.board.cells:
            cell.future_fire_start = {}
            cell.future_fire_end = {}
        # Update future fire tick values
        for cell in self.board.cells:
            if cell.bomb_diameter:
                self.board._on_bomb_placed(cell)
            #if cell.eog_fire and cell.created >= self.board.tick - 4: TODO RMA EOG FIRE
            #    if cell.x < cell.y:
            #        pass
            #    elif cell.x > cell.y:
            #        pass
            #    elif
        # Update unit->cell distances
        self.board._update_dists()
        self.board._update_target_range() # need dists first

        if self._tick_callback is not None:
            self.board.tick = game_tick.get("tick")
            await self._tick_callback(self.board)

        print(f'Tick {self.board.tick} handled in {round(1000 * (time.time() - tick_start_time))}ms')
        sa, sb = '', ''
        for unit_id, unit in self.board.units.items():
            if unit.player.id == 'a':
                sa = sa + f'{unit.hp} '
            else:
                sb = sb + f'{unit.hp} '
        print(f'A: {sa}, B: {sb}')

    def _on_unit_action(self, action_packet):
        '''Update units based on movement. Can ignore bomb/detonate actions (handled elsewhere)'''
        action_type = action_packet.get("type")
        if action_type == "move":
            unit_id = action_packet["unit_id"]
            self.board.units[unit_id]._on_unit_move(action_packet['move'])
