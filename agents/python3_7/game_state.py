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

TARGET_RANGE_LEN = 5 # was 10


class Cell:
    def __init__(self, board, position):
        self.board = board
        self.x = position % SIZE
        self.y = position // SIZE
        self.west, self.north, self.east, self.south = None, None, None, None
        self.safe_dists = {} # {unit_id: (dist, prev_cell)}
        self.safe_paths = None
        self.target_range = [0] * TARGET_RANGE_LEN # number of targets within range 1, 2, 3, 4...
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
        self.safety_scores = {}

    def copy(self, new_board):
        cell = Cell(new_board, self.y * SIZE + self.x)
        cell.hp = self.hp
        cell.wall = self.wall
        cell.box = self.box
        cell.created = self.created
        cell.expires = self.expires
        cell.bomb_diameter = self.bomb_diameter
        cell.fire = self.fire
        cell.blast_powerup = self.blast_powerup
        cell.freeze_powerup = self.freeze_powerup
        # Needs unit, bomb_unit
        cell.unit = None if not self.unit else self.unit.id
        cell.bomb_unit = None if not self.bomb_unit else self.bomb_unit.id
        return cell

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
        self.future_fire_start.clear()
        self.future_fire_end.clear()

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
        cells = random.sample([self.north, self.east, self.south, self.west], 4)
        return [cell for cell in cells
                if cell # cell exists
                and not cell.wall #and not cell.bomb_diameter and not cell.box  # No blocking entity
                and not (cell.unit and cell.unit.hp <= 0) # No dead unit
                and not (cell.unit and self.board.tick + 1 <= cell.unit.stunned)] # No stunned unit

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

        #safe_paths = safe_at_dist
        #print(f'unit {self.unit.id} safe paths:  ({self.unit.invulnerable} {self.unit.stunned})')
        #for i in range(len(safe_paths)):
        #    s = f'{i} ({len(safe_paths[i])}): '
        #    safe_paths[i].sort(key = lambda x: 100 * x.y + x.x)
        #    for c in safe_paths[i]:
        #        s = s + f'({c.x},{c.y}), '
        #    print(s)
                    
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
            self.bomb_unit.bombs.append(self)
            self.bomb_unit.player.bombs.append(self)
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
            # TODO: set next EOG fire future_fire values
            self.expires = 2000


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
        self.bombs = []

    def copy(self, new_board):
        unit = Unit(new_board, self.id)
        unit.x, unit.y = self.x, self.y
        unit.hp = self.hp
        unit.diameter = self.diameter
        unit.invulnerable = self.invulnerable
        unit.stunned = self.stunned
        # Need cell, player, bombs
        unit.player = self.player.id
        unit.bombs = [SIZE * bomb_cell.y + bomb_cell.x for bomb_cell in self.bombs]
        return unit

    def _update_dists(self):
        self.cell._update_safe_dists_to_all(self.id, self.player)
        #self.cell._update_safe_paths(self.id, self.player)

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
    def __init__(self, board, id):
        self.board = board
        self.id = id
        self.units = []
        self.bombs = []

    def copy(self, new_board):
        player = Player(new_board, self.id)
        return player # Still need units and bombs

    def get_hp_score(self):
        '''Per agent: 1 if full health, 0 if dead'''
        score = 0
        hp3_count = hp2_count = hp1_count = hp0_count = 0
        for unit in self.units:
            if unit.hp == 3:
                hp3_count += 1
            elif unit.hp == 2:
                hp2_count += 1
            elif unit.hp == 1:
                hp1_count += 1
            else:
                hp0_count += 1
        score += 1 * hp3_count
        score += 0.75 * hp2_count
        score += 0.45 * hp1_count
        score += 0 * hp0_count
        return score / 3

    def get_imminent_danger_score(self):
        '''Per unit: 1 if not in danger, 0 if in imminent danger'''
        score = 0
        opp_id = 'a' if self.id == 'b' else 'b'
        units = [u for u in self.units if u.hp > 0]
        for unit in units:
            safe_turns = unit.cell.safe_turns(unit.player, unit.invulnerable)
            if safe_turns < 4:
                score += 0.1 * max(0, safe_turns)
            else:
                if (self.id in unit.cell.future_fire_end
                    and unit.cell.future_fire_end[self.id] - 5 <= unit.stunned + 1):
                    continue # bad - 0 score
                if (opp_id in unit.cell.future_fire_start
                    and unit.cell.future_fire_start[opp_id] <= unit.stunned + 1):
                    continue # bad - 0 score
                score += 1
        return score / len(units) if len(units) else 0

    def get_dist_to_mining_goal_score(self):
        '''Per unit: 1 for being at mining site, 0 for being 10+ away'''
        score = 0
        units = [u for u in self.units if u.hp > 0]
        for unit in units:
            #if unit.cell.bomb_diameter:
            #    score += 0
            #    continue
            possible_goals = []
            for cell in self.board.cells:
                if cell.bomb_diameter or cell.wall or cell.box:
                    continue
                target_range_i = min(len(cell.target_range) - 1, ((unit.diameter // 2) - 1))
                target_range = cell.target_range[target_range_i]
                target_range = target_range if (unit.player.id == 'a') else -target_range
                if target_range >= 0.02:  # at least 2 ore boxes
                    safe_dist = cell.safe_dists[unit.id][0]
                    if safe_dist != UNREACHABLE:
                        possible_goals.append((cell, safe_dist, target_range))
            if possible_goals:
                possible_goals.sort(key=lambda x: x[2] / (x[1] + 6))
                #if self.id == 'b':
                #    print(f'mining goal - unit {unit.id} - {possible_goals}')
                score += max(0, 10 - possible_goals[-1][1])
            #else:
            #    score += 10
        return score / (10 * len(units)) if len(units) else 0

    def get_bomb_score(self):
        ''' '''
        # Want to make them drop a bomb
        # They are lured to site of mining/attacking
        # That value will decrease once the bomb is dropped
        # How to discourage bombing the "wrong" spots?
        # same scoring as mining goal: target_range / 6
        score = 0
        units = [u for u in self.units if u.hp > 0]
        for bomb_cell in self.bombs:
            target_range_i = min(len(bomb_cell.target_range) - 1, ((bomb_cell.bomb_diameter // 2) - 1))
            target_range = bomb_cell.target_range[target_range_i]
            target_range = target_range if (self.id == 'a') else -target_range
            if bomb_cell.unit:
                score += min(2, target_range % 10) / 2 # Want to step away
            else:
                score += min(2, target_range % 10)
        return score / (2 * 3) # 3 bombs, 2 max score per bomb
            
        

    def get_dist_to_freeze_powerup_goal_score(self):
        '''Per unit: 1 for being closest and at powerup, 0 for being 20+ away or not closest'''
        score = 0
        units = [u for u in self.units if u.hp > 0]
        close_units = {}
        for cell in self.board.cells:
            if cell.freeze_powerup and not cell.unit:
                min_unit_id, min_safe_dist = None, UNREACHABLE
                for unit_id in self.board.units:
                    safe_dist = cell.safe_dists[unit_id][0]
                    #print(f'freeze at {cell.x},{cell.y} dist {safe_dist} to unit {unit_id}')
                    if safe_dist < min_safe_dist:
                        min_unit_id, min_safe_dist = unit_id, safe_dist
                if min_unit_id is None:
                    continue
                if self.board.units[min_unit_id] in self.units:
                    if min_unit_id not in close_units or min_safe_dist < close_units[min_unit_id]:
                        close_units[min_unit_id] = min_safe_dist
        for close_unit_dist in close_units.values():
            score += max(0, 10 - close_unit_dist)
        return score / (20 * len(units)) if len(units) else 0

    def get_dist_to_blast_powerup_goal_score(self):
        '''Per unit: 1 for being closest and at powerup, 0 for being 10+ away or not closest'''
        score = 0
        units = [u for u in self.units if u.hp > 0]
        close_units = {}
        for cell in self.board.cells:
            if cell.blast_powerup and not cell.unit:
                min_unit_id, min_safe_dist = None, UNREACHABLE
                for unit_id in self.board.units:
                    safe_dist = cell.safe_dists[unit_id][0]
                    #print(f'blast at {cell.x},{cell.y} dist {safe_dist} to unit {unit_id}')
                    if safe_dist < min_safe_dist:
                        min_unit_id, min_safe_dist = unit_id, safe_dist
                if min_unit_id is None:
                    continue
                if self.board.units[min_unit_id] in self.units:
                    if min_unit_id not in close_units or min_safe_dist < close_units[min_unit_id]:
                        close_units[min_unit_id] = min_safe_dist
        for close_unit_dist in close_units.values():
            score += max(0, 10 - close_unit_dist)
        return score / (10 * len(units)) if len(units) else 0

    def get_opp_stun_score(self):
        '''Per opp unit: 0 is not stunned, 1 is stunned'''
        score = 0
        opp_id = 'a' if self.id == 'b' else 'b'
        opp_units = [u for u in self.board.players[opp_id].units if u.hp > 0]
        for opp_unit in opp_units:
            if opp_unit.stunned > opp_unit.invulnerable:
                score += 1
        return score / len(opp_units) if len(opp_units) else 0

    def get_blast_diameter_score(self):
        '''Per unit: 0 for 0 powerups, 1 for 10 powerups'''
        score = 0
        units = [u for u in self.units if u.hp > 0]
        for unit in units:
            score += min(10, (unit.diameter // 2) - 1)
        return score / (10 * len(units)) if len(units) else 0

    def get_safety_score(self):
        '''Per unit: 1 truly safe, 0 for future opp and near-term own'''
        opp_id = 'a' if self.id == 'b' else 'b'
        score = 0
        units = [u for u in self.units if u.hp > 0]
        for unit in units:
            score += unit.cell.safety_scores[self.id]
            #score += 1
            #if self.id in unit.cell.future_fire_start:
            #    score -= 0.3
            #    if unit.cell.future_fire_start[self.id] + 10 < self.board.tick + 1: # created+5 -> created+15
            #        score -= 0.2
            #if opp_id in unit.cell.future_fire_start:
            #    score -= 0.5
        return score / len(units) if len(units) else 0

    def get_score(self):
        '''
        -HP (L)
        -Imminent danger (L)
        Stun unit danger (L)

        Dist to stun attack opportunity (m-L)

        Near-term danger (m)
        Closest unit (and dist) to freeze powerup (m)
        Safety (m)
        Safety from own future fire (eesp if laid bomb) (m)
        Stunned units (m)
        safe_region comparison (m)

        Total unit blast diameters (s)
        Closest unit (and dist) to blast powerup (s)
        Closest unit to threatened boxes (s)
        Dist to box-blasting cell (s)
        '''
        hp_score = self.get_hp_score()
        imminent_danger_score = self.get_imminent_danger_score()
        dist_to_mining_goal_score = self.get_dist_to_mining_goal_score()
        dist_to_freeze_powerup_score = self.get_dist_to_freeze_powerup_goal_score()
        dist_to_blast_powerup_score = self.get_dist_to_blast_powerup_goal_score()
        blast_diameter_score = self.get_blast_diameter_score()
        opp_stun_score = self.get_opp_stun_score()
        bomb_score = self.get_bomb_score()
        safety_score = self.get_safety_score()
        scores = [(1000000, hp_score),
                  (10000, imminent_danger_score),
                  (1000, opp_stun_score),
                  (100, safety_score),
                  (300, dist_to_freeze_powerup_score),
                  (100, blast_diameter_score), # 0 -> 0.1 (+10)
                  (300, bomb_score),
                  (10, dist_to_blast_powerup_score), # 0.9 * 10 = 9 -> 0 (-9)
                  (1, dist_to_mining_goal_score),]
        score = 0
        for a, b in scores:
            score += a * b
        return score, scores
        


class Board:
    def __init__(self):
        self.tick = None
        self.agent_id = None
        self.cells = None
        self.players = None
        self.units = None
        self.player = None
        self.opp = None

    def init_from_game_state(self, game_state):
        self.tick = 0

        self.cells = [Cell(self, i) for i in range(SIZE2)]
        self.players = {player_id: Player(self, player_id) for player_id in ['a','b']}
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

        self.agent_id = game_state['connection']['agent_id']
        self.player = self.players['a'] if self.agent_id == 'a' else self.players['b']
        self.opp = self.players['a'] if self.agent_id == 'b' else self.players['b']

    def copy(self):
        board = Board()
        board.tick = self.tick
        board.agent_id = self.agent_id
        board.cells = [cell.copy(board) for cell in self.cells]
        board.players = {player_id: player.copy(board) for player_id, player in self.players.items()}
        board.units = {unit_id: unit.copy(board) for unit_id, unit in self.units.items()}
        board.player = board.players['a'] if board.agent_id == 'a' else board.players['b']
        board.opp = board.players['a'] if board.agent_id == 'b' else board.players['b']

        # Each Player needs .units, .bombs
        for new_player in board.players.values():
            for old_unit in self.players[new_player.id].units:
                new_player.units.append(board.units[old_unit.id])
            for old_bomb_cell in self.players[new_player.id].bombs:
                new_player.bombs.append(board.cells[SIZE * old_bomb_cell.y + old_bomb_cell.x])

        # Each cell needs .unit, .bomb_unit
        for new_cell in board.cells:
            new_cell._init_neighbors()
            if new_cell.unit:
                new_cell.unit = board.units[new_cell.unit] # .unit is initialized to id
            if new_cell.bomb_unit:
                new_cell.bomb_unit = board.units[new_cell.bomb_unit] # .bomb_unit initialized to id

        # Each Unit needs .cell, .player, .bombs
        for new_unit in board.units.values():
            new_unit.cell = board.cells[SIZE * new_unit.y + new_unit.x]
            new_unit.player = board.players[new_unit.player] # .player initialized to id
            new_unit.bombs = [board.cells[bomb_pos] for bomb_pos in new_unit.bombs]

        for new_cell in board.cells:
            new_cell.future_fire_start.clear()
            new_cell.future_fire_end.clear()
        for new_cell in board.cells:
            if new_cell.bomb_diameter:
                board._on_bomb_placed(new_cell)
        board._update_dists()
        board._update_target_range()

        return board

    def get_score(self, player_id):
        scorea, desca = self.players['a'].get_score()
        scoreb, descb = self.players['b'].get_score()
        score, desc = scorea - scoreb, desca
        if player_id == 'b':
            score *= -1
            desc = descb
        return score, desc

    def apply_detonation(self, blast_cell):
        # reduce box hp (remove box), remove powerup
        blast_cell.freeze_powerup = blast_cell.blast_powerup = False
        if blast_cell.wall:
            return # no fire
        if blast_cell.box:
            blast_cell.hp -= 1
            if blast_cell.hp == 0:
                blast_cell.box = False
                blast_cell.blast_powerup = True # TODO chance of freeze?
                return # no fire
        if blast_cell.bomb_diameter:
            self.units[blast_cell.bomb_unit.id].bombs.remove(blast_cell)
            self.players[blast_cell.bomb_unit.player.id].bombs.remove(blast_cell)
            blast_cell.bomb_diameter = blast_cell.bomb_unit = None
        if blast_cell.unit and blast_cell.unit.invulnerable < self.tick:
            blast_cell.unit.hp -= 1
            blast_cell.unit.invulnerable = self.tick + 5
            blast_cell.fire = True
            blast_cell.created = self.tick
            blast_cell.expires = self.tick + 5

    def apply_actions(self, actions):
        self.tick += 1
        detonate_actions = [a for a in actions if a[0] == 'detonate']
        bomb_actions = [a for a in actions if a[0] == 'bomb']
        move_actions = [a for a in actions if a[0] == 'move']

        for cell in self.cells:
            if cell.fire and cell.expires == self.tick:
                cell.fire = None
                cell.created = None
                cell.expires = None

        while detonate_actions:
            for action in detonate_actions:
                _, unit_id, det_x, det_y = action
                blast_cells = self.get_bomb_area(self.cells[SIZE * det_y + det_x])
                for blast_cell in blast_cells:
                    self.apply_detonation(blast_cell)
            detonate_actions = []
            for cell in self.cells:
                if cell.bomb_diameter and cell.expires == self.tick:
                    detonate_actions.append(('detonate2', cell.bomb_unit.id, cell.x, cell.y))

        for action in bomb_actions:
            _, unit_id = action
            unit = self.units[unit_id]
            if len(unit.player.bombs) == 3 or unit.cell.bomb_diameter:
                continue
            unit.cell.bomb_diameter = unit.diameter
            unit.cell.bomb_unit = unit
            unit.cell.created = self.tick
            unit.cell.expires = self.tick + 5
            unit.bombs.append(unit.cell)
            unit.player.bombs.append(unit.cell)
            if unit.cell.fire:
                blast_cells = self.get_bomb_area(unit.cell, diameter=unit.diameter)
                for blast_cell in blast_cells:
                    self.apply_detonation(blast_cell)

        remove_move_actions = set()
        for i in range(len(move_actions)):
            _, _, xi, yi = move_actions[i]
            for j in range(i + 1, len(move_actions)):
                _, _, xj, yj = move_actions[j]
                if xi == xj and yi == yj:
                    remove_move_actions.add(move_actions[i])
                    remove_move_actions.add(move_actions[j])
        for remove_move_action in remove_move_actions:
            move_actions.remove(remove_move_action)
        while True:
            move_uids = [uid for _, uid, _, _ in move_actions]
            remove_move_actions = set()
            for i, (_, _, x, y) in enumerate(move_actions):
                cell = self.cells[SIZE * y + x]
                if cell.wall or cell.box or cell.bomb_diameter or (cell.unit and not cell.unit.id in move_uids):
                    remove_move_actions.add(move_actions[i])
            for remove_move_action in remove_move_actions:
                move_actions.remove(remove_move_action)
            if not remove_move_actions:
                break
        for _, uid, x, y in move_actions:
            unit = self.units[uid]
            if unit.cell.unit == unit:
                unit.cell.unit = None
            unit.x = x
            unit.y = y
            self.cells[SIZE * y + x].unit = unit
            unit.cell = self.cells[SIZE * y + x]
            if unit.cell.fire and unit.invulnerable < self.tick:
                #print('SIMULATE FIRE DAMAGE FROM MOVEMENT')
                unit.hp -= 1
                unit.invulnerable = self.tick + 5
            elif unit.cell.blast_powerup:
                unit.cell.blast_powerup = False
                unit.diameter += 2
            elif unit.cell.freeze_powerup:
                unit.cell.freeze_powerup = False
                opp_id = 'a' if unit.player.id == 'b' else 'b'
                opp_units = [u for u in self.units.values()
                             if u.player.id == opp_id and u.hp > 0 and u.stunned < self.tick + 1]
                if opp_units:
                    stun_opp = random.choice(opp_units)
                    stun_opp.stunned = self.tick + 15

        for cell in self.cells:
            cell.future_fire_start = {}
            cell.future_fire_end = {}
        for cell in self.cells:
            if cell.bomb_diameter:
                self._on_bomb_placed(cell)
        self._update_dists()
        self._update_target_range()

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
                bomb_diameter = diameter if (diameter is not None and bomb_cell is cell) else bomb_cell.bomb_diameter
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
        #for cell in self.cells:
        #    cell.safe_paths = None
        for unit in self.units.values():
            unit._update_dists()
        for cell in self.cells:
            if (cell.fire or cell.wall or cell.box
                or cell.bomb_diameter
                or (cell.unit and cell.unit.stunned >= self.tick + 1)
                or (cell.unit and cell.unit.hp <= 0)):
                cell.safety_scores['a'] = 0
                cell.safety_scores['b'] = 0
            else:
                cell.safety_scores['a'] = 1
                cell.safety_scores['b'] = 1
                if 'a' in cell.future_fire_start:
                    cell.safety_scores['a'] -= 0.1
                    cell.safety_scores['b'] -= 0.5
                    if cell.future_fire_start['a'] + 10 < self.tick + 1:
                        cell.safety_scores['a'] -= 0.4
                if 'b' in cell.future_fire_start:
                    cell.safety_scores['b'] -= 0.1
                    cell.safety_scores['a'] -= 0.5
                    if cell.future_fire_start['b'] + 10 < self.tick + 1:
                        cell.safety_scores['b'] -= 0.4
        for _ in range(3):
            for cell in self.cells:
                if (cell.fire or cell.wall or cell.box or cell.bomb_diameter
                    or (cell.unit and cell.unit.stunned >= self.tick + 1)
                    or (cell.unit and cell.unit.hp <= 0)):
                    continue
                cell.safety_scores['a'] = (
                    0.6 * cell.safety_scores['a']
                    + (0.1 * cell.north.safety_scores['a'] if cell.north else 0)
                    + (0.1 * cell.west.safety_scores['a'] if cell.west else 0)
                    + (0.1 * cell.east.safety_scores['a'] if cell.east else 0)
                    + (0.1 * cell.south.safety_scores['a'] if cell.south else 0))
                cell.safety_scores['b'] = (
                    0.6 * cell.safety_scores['b']
                    + (0.1 * cell.north.safety_scores['b'] if cell.north else 0)
                    + (0.1 * cell.west.safety_scores['b'] if cell.west else 0)
                    + (0.1 * cell.east.safety_scores['b'] if cell.east else 0)
                    + (0.1 * cell.south.safety_scores['b'] if cell.south else 0))


    def _update_target_range(self):
        '''Positive if it favors player a, negative for player b'''
        for cell in self.cells:
            for i in range(len(cell.target_range)):
                cell.target_range[i] = 0
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
        self.board = Board()
        self.board.init_from_game_state(game_state)
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
            cell.future_fire_start.clear()
            cell.future_fire_end.clear()
        # Update future fire tick values
        for cell in self.board.cells:
            if cell.bomb_diameter:
                self.board._on_bomb_placed(cell)
        # Update unit->cell distances
        self.board._update_dists()
        self.board._update_target_range() # need dists first

        if self._tick_callback is not None:
            self.board.tick = game_tick.get("tick")
            await self._tick_callback(self.board)

        #print(f'Tick {self.board.tick} handled in {round(1000 * (time.time() - tick_start_time))}ms')
        #sa, sb = '', ''
        #for unit_id, unit in self.board.units.items():
        #    if unit.player.id == 'a':
        #        sa = sa + f'{unit.hp} '
        #    else:
        #        sb = sb + f'{unit.hp} '
        #print(f'A: {sa}, B: {sb}')

    def _on_unit_action(self, action_packet):
        '''Update units based on movement. Can ignore bomb/detonate actions (handled elsewhere)'''
        action_type = action_packet.get("type")
        if action_type == "move":
            unit_id = action_packet["unit_id"]
            self.board.units[unit_id]._on_unit_move(action_packet['move'])
