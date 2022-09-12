'''
Action process order:
 - Bomb placement
 - Bomb detonation
 - Movement

If bomb is placed on cell with fire, it detonates immediately.
If unit1 places bomb next to unit2's bomb at same time unit2 detonates, detonates immediately.
  TODO: worthwhile if can damage 2+ opp units
Stunned units cannot detonate bombs.
  TODO: update future_fire_start for stunned units' bombs

TODO: update future_fire_start for bomb->bomb detonations

Score moves/goals for opponent units too. I also want to go to there.
  e.g. move to same cell opp wants to go to to escape future fire, blocking the move

Maybe do full cell->cell distance calculation?
  - or at least do full dist calculation for each move cell candidate
    - Helpful to see relative distances to target for different move cells

'''

from game_state import GameState, SIZE, UNREACHABLE
import asyncio
import random
import os
import time

uri = os.environ.get(
    'GAME_CONNECTION_STRING') or "ws://127.0.0.1:3000/?role=agent&agentId=agentId&name=defaultName"


def check_for_flee_danger_goal(board, unit):
    if unit.cell.safe_turns(unit.player, unit.invulnerable) != 1:
        return None
    for dist in range(len(unit.cell.safe_paths) - 1, 0, -1): # backwards, don't check dist==0
        for safe_cell in unit.cell.safe_paths[dist]:
            return safe_cell # Return furthest safe destination
    return None

def can_detonate_for_damage(board, unit, bomb_cell, diameter=None): # TODO: coordinate with this tick's moves
    if bomb_cell.created and board.tick < bomb_cell.created + 5:
        return False

    blast_cells = board.get_bomb_area(bomb_cell, diameter=diameter)
    units_hit = set()
    for blast_cell in blast_cells:
        if blast_cell.unit and blast_cell.unit.invulnerable < board.tick + 1:
            units_hit.add(blast_cell.unit)
        if blast_cell.unit_next and blast_cell.unit_next.invulnerable < board.tick + 1:
            units_hit.add(blast_cell.unit_next)

    detonate_score = 0
    for unit_hit in units_hit:
        ds = 0
        if unit_hit.hp == 3:
            ds = 10
        elif unit_hit.hp == 2:
            ds = 11
        elif unit_hit.hp == 1:
            ds = 20
        detonate_score += (-ds if (unit_hit.player is unit.player) else ds)
    return detonate_score > 1

def can_bomb_safely(board, unit):
    # assume valid bombing opp
    # get_bomb_area
    # get safe_paths. 
    # only safe if a cell in safe_paths exists outside of bomb_area
    blast_cells = board.get_bomb_area(unit.cell, unit.diameter)
    for dist in range(1, len(unit.cell.safe_paths)):
        for cell in unit.cell.safe_paths[dist]:
            if not cell.future_fire_start and not cell in blast_cells:
                return True
    return False
    

def can_bomb_for_stunned_opp(board, unit):
    if unit.cell.bomb_diameter or len(unit.player.bombs) == 3:
        return False
    if (unit.cell.fire and unit.cell.expires > board.tick + 1 # still on fire next turn
        and unit.invulnerable < board.tick + 3):  # Invulnerable in < 3 turns (explosion + 2 escape moves)
        return False
    if not can_bomb_safely(board, unit):
        return False
    opp_id = 'a' if unit.player.id == 'b' else 'b'
    opp_stun_cells = []
    for opp_unit in board.players[opp_id].units:
        if (opp_unit.hp > 0  # not dead
            and opp_unit.stunned >= board.tick + 1 + 5):  # still stunned when bomb can go off
            opp_stun_cells.append(opp_unit.cell)
    if not opp_stun_cells:
        return False
    blast_cells = board.get_bomb_area(unit.cell, diameter=unit.diameter)
    for cell in opp_stun_cells:
        if cell in blast_cells:
            return True
    return False

def can_bomb_for_instant_damage(board, unit):
    if unit.cell.bomb_diameter or len(unit.player.bombs) == 3:
        return False
    if not can_bomb_safely(board, unit):
        return False
    if (unit.cell.fire
        and unit.cell.expires > board.tick + 1     # Still on fire next turn
        and unit.invulnerable >= board.tick + 3):  # Invulnerable in 3 turns (explosion + 2 escape moves)
        if can_detonate_for_damage(board, unit, unit.cell, unit.diameter):
            return True
    return False

def check_for_stun_attack_goal(board, unit):
    opp_id = 'a' if unit.player.id == 'b' else 'b'
    opp_stun_cells = []
    for opp_unit in board.players[opp_id].units:
        if (opp_unit.hp > 0  # not dead
            and opp_unit.stunned >= board.tick + 1 + 5):  # still stunned when bomb can go off
            opp_stun_cells.append(opp_unit.cell)
    if not opp_stun_cells:
        return None
    for bomb_cell in unit.player.bombs:
        blast_cells = board.get_bomb_area(bomb_cell)
        for blast_cell in blast_cells:
            if blast_cell in opp_stun_cells:
                opp_stun_cells.remove(blast_cell)
    if not opp_stun_cells:
        return None
    for cell in board.cells:
        target_range_i = min(len(cell.target_range) - 1, ((unit.diameter // 2) - 1))
        target_range = cell.target_range[target_range_i]
        target_range = target_range if (unit.player.id == 'a') else -target_range
        if target_range > 10:  # stun attacks are 20
            blast_cells = board.get_bomb_area(cell, diameter=unit.diameter)
            for opp_stun_cell in opp_stun_cells:
                safe_dist = cell.safe_dists[unit.id][0]
                if opp_stun_cell.unit.stunned >= board.tick + 1 + 5 + safe_dist:
                    return cell
    return None

def check_for_freeze_powerup_goal(board, unit):
    for cell in board.cells:
        if cell.freeze_powerup and not cell.unit:
            min_unit_id, min_safe_dist = None, UNREACHABLE
            for unit_id in board.units:
                safe_dist = cell.safe_dists[unit_id][0]
                if safe_dist < min_safe_dist:
                    min_unit_id, min_safe_dist = unit_id, safe_dist
            print(f'FREEZE: unit {min_unit_id} is {min_safe_dist} ticks away')
            #if unit.player is board.player['b']:
            #    print(f'unit {unit.id} dist={cell.safe_dists[unit.id][0]}')
            if min_unit_id == unit.id:
                return cell
    return None

def check_for_blast_powerup_goal(board, unit):
    for cell in board.cells:
        if cell.blast_powerup and not cell.unit:
            min_unit_id, min_safe_dist = None, UNREACHABLE
            for unit_id in board.units:
                safe_dist = cell.safe_dists[unit_id][0]
                if safe_dist < min_safe_dist:
                    min_unit_id, min_safe_dist = unit_id, safe_dist
            print(f'BLAST: unit {min_unit_id} is {min_safe_dist} ticks away')
            if min_unit_id == unit.id:
                return cell
    return None

def check_for_detonation_safety_goal(board, unit):
    if unit.bombs and unit.player.id in unit.cell.future_fire_start:
        for dist in range(len(unit.cell.safe_paths) - 1, 0, -1):
            for cell in unit.cell.safe_paths[dist]:
                if not cell.future_fire_start:
                    return cell
        #for dist in range(1, len(unit.cell.safe_paths)):
        #    for cell in unit.cell.safe_paths[dist]:
        #        if not unit.player.id in cell.future_fire_start: # Safely navigate to opp-bomb territory as fallback?
        #            return cell

def check_for_choke_point_goal(board, unit):
    opp_id = 'a' if unit.player.id == 'b' else 'b'

    choke_points = []
    for opp_unit in board.players[opp_id].units:
        if opp_unit.hp <= 0:
            continue

        # If range is already restricted, ignore
        if len(opp_unit.cell.safe_paths[-1]) <= 1:
            continue

        # Identify choke points for opp_unit
        for i in range(1, len(opp_unit.cell.safe_paths)):
            if len(opp_unit.cell.safe_paths[i]) == 1:
                for j in range(i + 1, len(opp_unit.cell.safe_paths)):
                    if len(opp_unit.cell.safe_paths[i]) > 1:
                        choke_points.append((opp_unit.cell.safe_paths[i][0], i))

    for choke_cell, opp_dist in choke_points:
        if choke_cell.safe_dists[unit.id][0] <= opp_dist:
            return choke_cell
    return None

def can_detonate_for_no_damage(board, unit, bomb_cell): # TODO: coordinate with this tick's moves
    if bomb_cell.created and board.tick < bomb_cell.created + 5:
        return False

    blast_cells = board.get_bomb_area(bomb_cell)
    units_hit = set()
    for blast_cell in blast_cells:
        if blast_cell.unit and blast_cell.unit.invulnerable < board.tick + 1:
            units_hit.add(blast_cell.unit)
        if blast_cell.unit_next and blast_cell.unit_next.invulnerable < board.tick + 1:
            units_hit.add(blast_cell.unit_next)

    #detonate_score = 0
    #for unit_hit in units_hit:
    #    ds = 0
    #    if unit_hit.hp == 3:
    #        ds = 10
    #    elif unit_hit.hp == 2:
    #        ds = 11
    #    elif unit_hit.hp == 1:
    #        ds = 20
    #    detonate_score += (-ds if (unit_hit.player is unit.player) else ds)
    #return detonate_score == 0
    return len(units_hit) == 0

def check_for_mining_goal(board, unit):
    possible_goals = []
    for cell in board.cells:
        if cell.bomb_diameter or cell.wall or unit.player.id in cell.future_fire_start:
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
        return possible_goals[-1][0]
    return None

def check_for_any_safe_goal(board, unit):
    for dist in range(len(unit.cell.safe_paths) - 1, 0, -1): # backwards, don't check dist==0
        for safe_cell in unit.cell.safe_paths[dist]:
            return safe_cell # Return furthest safe destination
    return unit.cell

def can_bomb_for_mining(board, unit):
    if unit.cell.bomb_diameter or len(unit.player.bombs) == 3:
        return False
    if (unit.cell.fire and unit.cell.expires > board.tick + 1 # still on fire next turn
        and unit.invulnerable < board.tick + 3):  # Invulnerable in < 3 turns (explosion + 2 escape moves)
        return False
    if not can_bomb_safely(board, unit):
        return False
    mining_goal = check_for_mining_goal(board, unit)
    return mining_goal is unit.cell

async def do_move(board, unit, dest_cell):
    # TODO avoid cells with cell.unit_next populated 
    if unit.cell is dest_cell:
        print('! do_move to same cell')
        return

    cell = dest_cell
    while cell:
        prev_cell = cell.safe_dists[unit.id][1]
        if prev_cell is unit.cell:
            break
        cell = prev_cell

    if not cell:
        print(f'WARNING!!! bad goal for unit {unit.id}')
        move_cell = unit.cell
    else:
        move_cell = unit.cell
        safe_turns = cell.safe_turns(unit.player, unit.invulnerable)
        print(f'SAFE_TURNS: {safe_turns} safe turns(s) at {cell.x},{cell.y}')
        if safe_turns >= 2:
            move_cell = cell
    
    #move_scores = []
    #for move_cell in unit.cell.move_neighbors():
    #    safe_dist = move_cell.get_safe_dist(dest_cell, unit.player, unit.invulnerable)
    #    norm_dist = move_cell.get_dist(dest_cell, unit.player)
    #    move_scores.append((safe_dist, norm_dist, random.random(), move_cell))
    #move_scores.sort()
    #move_cell = move_scores[0][3]

    ACTIONS = {
        unit.cell: None,
        unit.cell.west:  'left',
        unit.cell.east:  'right',
        unit.cell.north: 'up',
        unit.cell.south: 'down',
    }
    action = ACTIONS[move_cell]
    move_cell.unit_next = unit
    print(f'unit {unit.id} do_move {dest_cell.x},{dest_cell.y} via {move_cell.x},{move_cell.y} ({action})')
    if action:
        await board._client.send_move(action, unit.id)

async def do_bomb(board, unit):
    # Update future_fire_start for unit's player
    blast_cells = board.get_bomb_area(unit.cell, unit.diameter)
    for blast_cell in blast_cells:
        if not unit.player.id in blast_cell.future_fire_start:
            blast_cell.future_fire_start[unit.player.id] = board.tick + 1 + 5
            blast_cell.future_fire_end[unit.player.id] = board.tick + 1 + 30 + 5
        else:
            blast_cell.future_fire_start[unit.player.id] = (
                min(board.tick + 1 + 5, blast_cell.future_fire_start[unit.player.id]))
            blast_cell.future_fire_end[unit.player.id] = (
                min(board.tick + 1 + 30 + 5, blast_cell.future_fire_end[unit.player.id]))
    unit.bombs.append(unit.cell)
    unit.player.bombs.append(unit.cell)
    unit.cell.bomb_diameter = unit.diameter
    unit.cell.bomb_unit = unit
    print(f'unit {unit.id} do_bomb {unit.cell.x},{unit.cell.y}')
    await board._client.send_bomb(unit.id)

async def do_detonate(board, unit, bomb_cell):
    # TODO get blast_cells and set future_fire for board.player.id
    blast_cells = board.get_bomb_area(unit.cell, unit.diameter)
    for blast_cell in blast_cells:
        if not unit.player.id in blast_cell.future_fire_start:
            blast_cell.future_fire_start[unit.player.id] = board.tick + 1
            blast_cell.future_fire_end[unit.player.id] = board.tick + 1 + 5
        else:
            blast_cell.future_fire_start[unit.player.id] = (
                min(board.tick + 1 + 5, blast_cell.future_fire_start[unit.player.id]))
            blast_cell.future_fire_end[unit.player.id] = (
                min(board.tick + 1 + 5, blast_cell.future_fire_end[unit.player.id]))
    unit.bombs.remove(bomb_cell)
    unit.player.bombs.remove(bomb_cell)
    bomb_cell.bomb_diameter = None
    bomb_cell.bomb_unit = None
    print(f'unit {unit.id} do_detonate {bomb_cell.x},{bomb_cell.y}')
    await board._client.send_detonate(bomb_cell.x, bomb_cell.y, unit.id)

async def act(board, units): # TODORMA

    # TODO goal of move to center if near outside late in game (after 200)
    for player in board.players.values():
        player.save_bombs = list(player.bombs)
    for unit in board.units.values():
        unit.save_bombs = list(unit.bombs)
    for cell in board.cells:
        cell.save_bomb_diameter = cell.bomb_diameter
        cell.save_bomb_unit = cell.bomb_unit

    units_done = []
    for unit in [u for u in units if u not in units_done]:
        flee_danger_goal = check_for_flee_danger_goal(board, unit)
        if flee_danger_goal:
            print(f'unit {unit.id} flee danger')
            await do_move(board, unit, flee_danger_goal)
            units_done.append(unit)
    for unit in [u for u in units if u not in units_done]:
        for bomb_cell in unit.bombs:
            if can_detonate_for_damage(board, unit, bomb_cell):
                print(f'unit {unit.id} detonate for damage')
                await do_detonate(board, unit, bomb_cell)
                units_done.append(unit)
                break # out of bomb loop
    for unit in [u for u in units if u not in units_done]:
        if can_bomb_for_stunned_opp(board, unit):
            print(f'unit {unit.id} bomb for stunned opp')
            await do_bomb(board, unit)
            units_done.append(unit)
    for unit in [u for u in units if u not in units_done]:
        if can_bomb_for_instant_damage(board, unit):
            print(f'unit {unit.id} bomb for instant damage')
            await do_bomb(board, unit)
            units_done.append(unit)
    for unit in [u for u in units if u not in units_done]:
        stun_attack_goal = check_for_stun_attack_goal(board, unit)
        if stun_attack_goal:
            print(f'unit {unit.id} goal: stun attack {stun_attack_goal.x},{stun_attack_goal.y}')
            await do_move(board, unit, stun_attack_goal)
            units_done.append(unit)
    for unit in [u for u in units if u not in units_done]:
        freeze_powerup_goal = check_for_freeze_powerup_goal(board, unit)
        if freeze_powerup_goal:
            print(f'unit {unit.id} goal: freeze powerup {freeze_powerup_goal.x},{freeze_powerup_goal.y}')
            await do_move(board, unit, freeze_powerup_goal)
            units_done.append(unit)
    # TODO can_bomb_for_opp_disruption
    for unit in [u for u in units if u not in units_done]:
        blast_powerup_goal = check_for_blast_powerup_goal(board, unit)
        if blast_powerup_goal:
            print(f'unit {unit.id} goal: blast powerup {blast_powerup_goal.x},{blast_powerup_goal.y}')
            await do_move(board, unit, blast_powerup_goal)
            units_done.append(unit)
    for unit in [u for u in units if u not in units_done]:
        detonation_safety_goal = check_for_detonation_safety_goal(board, unit)
        if detonation_safety_goal:
            print(f'unit {unit.id} goal: detonation safety {detonation_safety_goal.x},{detonation_safety_goal.y}')
            await do_move(board, unit, detonation_safety_goal)
            units_done.append(unit)
    for unit in [u for u in units if u not in units_done]:
        choke_point_goal = check_for_choke_point_goal(board, unit)
        if choke_point_goal:
            print(f'unit {unit.id} goal: choke point {choke_point_goal.x},{choke_point_goal.y}')
            await do_move(board, unit, choke_point_goal)
            units_done.append(unit)
    for unit in [u for u in units if u not in units_done]:
        for bomb_cell in unit.bombs:
            assert bomb_cell.bomb_diameter
            if can_detonate_for_no_damage(board, unit, bomb_cell):
                print(f'unit {unit.id} detonate for no damage')
                await do_detonate(board, unit, bomb_cell)
                units_done.append(unit)
                break # out of bomb loop
    for unit in [u for u in units if u not in units_done]:
        if can_bomb_for_mining(board, unit):
            print(f'unit {unit.id} bomb for mining')
            await do_bomb(board, unit)
            units_done.append(unit)
    for unit in [u for u in units if u not in units_done]:
        mining_goal = check_for_mining_goal(board, unit)
        if mining_goal:
            print(f'unit {unit.id} goal: mining {mining_goal.x},{mining_goal.y}')
            await do_move(board, unit, mining_goal)
            units_done.append(unit)
    for unit in [u for u in units if u not in units_done]:
        any_safe_goal = check_for_any_safe_goal(board, unit)
        if any_safe_goal:
            print(f'unit {unit.id} goal: anything safe {any_safe_goal.x},{any_safe_goal.y}')
            await do_move(board, unit, any_safe_goal)
            units_done.append(units)
    assert len(units) == len(units_done)

    for player in board.players.values():
        player.bombs = list(player.save_bombs)
    for unit in board.units.values():
        unit.bombs = list(unit.save_bombs)
    for cell in board.cells:
        cell.bomb_diameter = cell.save_bomb_diameter
        cell.bomb_unit = cell.save_bomb_unit


class Agent():
    def __init__(self):
        self._client = GameState(uri)
        self._client.set_game_tick_callback(self._on_game_tick)
        loop = asyncio.get_event_loop()
        connection = loop.run_until_complete(self._client.connect())
        tasks = [asyncio.ensure_future(self._client._handle_messages(connection))]
        loop.run_until_complete(asyncio.wait(tasks))

    async def _on_game_tick(self, board):
        for cell in board.cells:
            cell.unit_next = None

        units = [u for u in board.player.units if u.hp > 0 and u.stunned < board.tick]
        await act(board, units)
        return



def main():
    for i in range(0,10):
        while True:
            try:
                Agent()
            except:
                time.sleep(5)
                continue
            break


if __name__ == "__main__":
    main()
