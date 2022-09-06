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
        self.box_range = [0, 0, 0, 0] # number of boxes within range 1, 2, 3, 4 # TODO: by hp too?
        self.unit = None
        self.score = [0, 0, 0, 0] # indexed by ((unit.blast_diameter // 2) - 1)
        self.hp = 0
        self.wall = False # Only true for indestructible walls
        self.box = False # Only true for destructible wooden/ore blocks
        self.created = None
        self.expires = None
        self.bomb = None # int: blast diameter of bomb
        self.bomb_unit = None
        self.fire = None # bool
        self.blast_powerup = None
        self.freeze_powerup = None
        # TODO: future_fire_start/expires['a'] and future_fire_start/expires['b']
        self.future_fire_start = None # tick that fire may start
        self.future_fire_end = None # tick that fire may last until

    def _on_entity_expired(self):
        self.hp = 0
        self.wall = False
        self.box = False
        self.created = None
        self.expires = None
        self.bomb = None
        self.bomb_unit = None
        self.fire = None
        self.blast_powerup = None
        self.freeze_powerup = None
        self.future_fire_start = None
        self.future_fire_end = None

    def neighbor(self, dx, dy):
        x, y = self.x + dx, self.y + dy
        if 0 <= x < SIZE and 0 <= y < SIZE:
            return self.board.cell(x, y)
        return None

    def search_neighbors(self, player):
        cells = random.sample([self.north, self.east, self.south, self.west], 4)
        return [cell for cell in cells
                if cell and not cell.wall and (not cell.unit or cell.unit.player is player)]

    def move_neighbors(self):
        cells = random.sample([self.north, self.east, self.south, self.west, self], 5)
        return [cell for cell in cells
                if cell and not cell.wall and not cell.bomb and not cell.box]

    def get_dist(self, other_cell, player):
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
                    new_dist += 6 * new_cell.hp

                arrival_tick = self.board.tick + new_dist
                if (new_cell.future_fire_end
                    and new_cell.future_fire_start <= arrival_tick < new_cell.future_fire_end):
                    new_dist = new_cell.future_fire_end - self.board.tick

                if new_dist < new_cell.dists[temp_id][0]:
                    new_cell.dists[temp_id] = (new_dist, cell)
                    heapq.heappush(queue, (new_dist, random.random(), new_cell))
        return UNREACHABLE

    def _update_dists_to_all(self, unit_id, player):
        assert self.unit and self.unit.id == unit_id

        for cell in self.board.cells:
            cell.dists[unit_id] = (UNREACHABLE, None)

        self.dists[unit_id] = (0, None)
        queue = [(0, 0, self)]
        while queue:
            dist, _, cell = heapq.heappop(queue)
            for new_cell in cell.search_neighbors(player):
                new_dist = dist + 1
                if new_cell.box:
                    new_dist += 6 * new_cell.hp

                arrival_tick = self.board.tick + new_dist
                if (new_cell.future_fire_end
                    and new_cell.future_fire_start <= arrival_tick < new_cell.future_fire_end):
                    new_dist = new_cell.future_fire_end - self.board.tick

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

    def _update_score(self):
        score = 0
        if self.blast_powerup:
            score += 10
        if self.freeze_powerup:
            score += 20
        self.score = [score] * 4
        for i in range(len(self.score)):
            self.score[i] += self.box_range[i]
        #print(f'{self.x},{self.y}: {score} {self.score} {self.box_range}')

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
            self.bomb = payload['blast_diameter']
            self.bomb_unit = self.board.units[payload['unit_id']]
            self.board._on_bomb_placed(self)
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

    def set_goal_list(self):
        '''Return list of three (cell, score) pairs in order'''
        # Function of cell score and distance
        # TODO will also need to work in bomb placement/detonation at some point        
        goal_list = []
        for cell in self.board.cells:
            cell_score_i = min(len(cell.score) - 1, ((self.diameter // 2) - 1))
            cell_score = cell.score[cell_score_i]
            cell_dist =  cell.dists[self.id][0]
            #goal_score = cell_score * (max(1, 100 - cell_dist) / 100)
            goal_score = cell_score * (0.9 ** cell_dist)
            goal_list.append((goal_score, cell))
        goal_list.sort(key=lambda x: x[0], reverse=True)
        self.goal_list = goal_list[:3]

    def _update_dists(self):
        self.cell._update_dists_to_all(self.id, self.player)

    def _on_unit_state(self, payload):
        if self.cell and self.cell.unit == self:
            self.cell.unit = None
        self.x, self.y = payload['coordinates']
        self.cell = self.board.cells[self.y * SIZE + self.x]
        self.cell.unit = self
        self.player = self.board.player_a if payload['agent_id'] == 'a' else self.board.player_b
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


class Board:
    def __init__(self, game_state):
        self.tick = 0

        self.cells = [Cell(self, i) for i in range(SIZE2)]
        self.player_a = Player('a')
        self.player_b = Player('b')

        self.units = {unit_id: Unit(self, unit_id) for unit_id in ['c','d','e','f','g','h']}
        for unit_id in game_state['unit_state']:
            self.units[unit_id]._on_unit_state(game_state['unit_state'][unit_id])
        for unit_id in game_state['agents']['a']['unit_ids']:
            self.player_a.units.append(self.units[unit_id])
        for unit_id in game_state['agents']['b']['unit_ids']:
            self.player_b.units.append(self.units[unit_id])

        for cell in self.cells:
            cell._init_neighbors()
        for entity in game_state['entities']:
            self._on_entity_spawned(entity)

        agent_id = game_state['connection']['agent_id']
        self.player = self.player_a if agent_id == 'a' else self.player_b
        self.opp = self.player_a if agent_id == 'b' else self.player_b

    def cell(self, x, y):
        return self.cells[y * SIZE + x]

    def _update_dists(self):
        for unit in self.units.values():
            unit._update_dists()

    def _update_box_range(self):
        for cell in self.cells:
            cell.box_range = [0, 0, 0, 0]
        for cell in self.cells:
            if cell.wall or cell.box or cell.bomb:
                continue
            for direction in ('north', 'south', 'east', 'west'):
                nearby_cell = cell
                for dist in range(4):
                    nearby_cell = getattr(nearby_cell, direction)
                    if not nearby_cell or nearby_cell.wall:
                        break
                    if nearby_cell.box:
                        #if nearby_cell.hp == 1: # TODO somehow handle higher hps
                        for i in range(dist, len(cell.box_range)):
                            cell.box_range[i] += 1 / (10 ** (nearby_cell.hp - 1))
                        break

    def _on_bomb_placed(self, cell):
        '''Can be called more than once, and on different ticks'''
        start, end = cell.created + 5, cell.expires + 5
        radius = (cell.bomb // 2) + 1
        def _set_future_fire(cell, count, direction):
            if cell is None or count == 0 or cell.box or cell.wall:
                return
            cell.future_fire_start, cell.future_fire_end = start, end
            _set_future_fire(getattr(cell, direction), count - 1, direction)
        for direction in ('north', 'south', 'east', 'west'):
            _set_future_fire(cell, radius, direction)

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
            cell.future_fire_start = cell.future_fire_end = None
        # Update future fire tick values
        for cell in self.board.cells:
            if cell.bomb:
                self.board._on_bomb_placed(cell)
        # Update cell score values
        for cell in self.board.cells:
            cell._update_score()
        #raise TypeError('x')
        # Update unit->cell distances
        self.board._update_dists()
        self.board._update_box_range()

        if self._tick_callback is not None:
            self.board.tick = game_tick.get("tick")
            await self._tick_callback(self.board)

        #print(f'Tick {self.board.tick} handled in {round(1000 * (time.time() - tick_start_time))}ms')

    def _on_unit_action(self, action_packet):
        '''Update units based on movement. Can ignore bomb/detonate actions (handled elsewhere)'''
        action_type = action_packet.get("type")
        if action_type == "move":
            unit_id = action_packet["unit_id"]
            self.board.units[unit_id]._on_unit_move(action_packet['move'])
