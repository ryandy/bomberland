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

g_tick_start = None

def goal_is_safe(board, unit, dest_cell):
    #return True
    #if dest_cell is unit.cell:
    #    safe_paths = unit.cell.safe_paths
    #else:
    if not dest_cell.future_fire_start:
        return True
    #start_time = time.time()
    safe_paths, num_safe, num_truly_safe, max_safe_dist = dest_cell.get_safe_paths(unit.player, unit.invulnerable)
    #print(f'GOAL_IS_SAFE GET_SAFE_PATHS TIME {round((time.time() - start_time) * 1000)}')
    return num_truly_safe > 0

def check_for_flee_danger_goal(board, unit):
    safe_turns = unit.cell.safe_turns(unit.player, unit.invulnerable)
    if safe_turns < 1 or safe_turns > 2:
        return None
    for dist in range(len(unit.cell.safe_paths) - 1, 0, -1): # backwards, don't check dist==0
        for safe_cell in unit.cell.safe_paths[dist]:
            if not safe_cell.future_fire_start:
                return safe_cell # Return furthest truly safe destination
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
    blast_cells = board.get_bomb_area(unit.cell, diameter=unit.diameter)
    own_units = [u for u in unit.player.units if u.hp > 0 and u.cell in blast_cells]
    safe_units = set()
    assert unit in own_units
    for own_unit in own_units:
        for dist in range(1, len(own_unit.cell.safe_paths)):
            for cell in own_unit.cell.safe_paths[dist]:
                if not cell.future_fire_start and not cell in blast_cells:
                    safe_units.add(own_unit)
                    break
    return len(safe_units) == len(own_units)

def is_stunned_opp_already_threatened(board, unit, opp_cell):
    assert opp_cell.unit and opp_cell.unit.stunned > board.tick
    bomb_cells = board.players['a'].bombs + board.players['b'].bombs
    for bomb_cell in bomb_cells:
        blast_cells = board.get_bomb_area(bomb_cell)
        if opp_cell in blast_cells:
            expires = bomb_cell.expires if bomb_cell.expires else board.tick + 1 + 30
            if expires <= opp_cell.unit.stunned + 1: # Any bomb has been placed and will expire in time
                return True
            if not bomb_cell.expires:
                return False # definitely not this unit (bomb will be laid next turn by other own unit)
            if bomb_cell.bomb_unit.id == unit.id: # This unit already has a bomb placed
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
            if not is_stunned_opp_already_threatened(board, unit, opp_unit.cell):
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
        if can_detonate_for_damage(board, unit, unit.cell, diameter=unit.diameter):
            return True
    return False

def check_for_stun_attack_goal(board, unit):
    opp_id = 'a' if unit.player.id == 'b' else 'b'
    opp_stun_cells = []
    for opp_unit in board.players[opp_id].units:
        if (opp_unit.hp > 0  # not dead
            and opp_unit.stunned >= board.tick + 1 + 5):  # still stunned when bomb can go off
            if not is_stunned_opp_already_threatened(board, unit, opp_unit.cell):
                opp_stun_cells.append(opp_unit.cell)
    if not opp_stun_cells:
        return None

    #for bomb_cell in unit.player.bombs:
    #    blast_cells = board.get_bomb_area(bomb_cell)
    #    for blast_cell in blast_cells:
    #        if blast_cell in opp_stun_cells:
    #            opp_stun_cells.remove(blast_cell)
    #already_threatened = []
    #for opp_stun_cell in opp_stun_cells:
    #    if is_stunned_opp_already_threatened(board, unit, opp_stun_cell):
    #        already_threatened.append(opp_stun_cell)
    #for x in already_threatened:
    #    opp_stun_cells.remove(x)
    #if not opp_stun_cells:
    #    return None

    # TODO get bomb area for all cells with close enough dist, rather than rely on target_range
    
    #for cell in board.cells:
    #    target_range_i = min(len(cell.target_range) - 1, ((unit.diameter // 2) - 1))
    #    target_range = cell.target_range[target_range_i]
    #    target_range = target_range if (unit.player.id == 'a') else -target_range
    #    if target_range > 10:  # stun attacks are 20
    #        blast_cells = board.get_bomb_area(cell, diameter=unit.diameter)
    #        for opp_stun_cell in opp_stun_cells:
    #            safe_dist = cell.safe_dists[unit.id][0]
    #            if opp_stun_cell.unit.stunned >= board.tick + 1 + 5 + safe_dist:
    #                if goal_is_safe(board, unit, cell):
    #                    return cell

    possible_goals = []
    for cell in board.cells:
        #if (cell.wall or cell.box or cell.bomb_diameter
        #    or (cell.unit and cell.unit.hp <= 0)
        #    or (cell.unit and cell.unit.player.id != unit.player.id)):
        #    continue

        safe_dist = cell.safe_dists[unit.id][0]
        if safe_dist > 25: # stun duration - bomb place/priming
            continue

        own_max, opp_max = 0, 0
        opp_count = 0
        blast_cells = board.get_bomb_area(cell, diameter=unit.diameter)
        for blast_cell in blast_cells:
            if blast_cell.unit and blast_cell.unit.stunned >= board.tick + 1 + 5 + safe_dist:
                if blast_cell.unit.player.id == unit.player.id:
                    own_max = max(own_max, blast_cell.unit.stunned)
                else:
                    opp_max = max(opp_max, blast_cell.unit.stunned)
                    opp_count += 1
        if opp_count and opp_max > own_max:
            possible_goals.append((cell, opp_count, safe_dist))

    possible_goals.sort(key=lambda x: x[1] / (x[2] + 0.1), reverse=True)
    for possible_goal in possible_goals:
        if goal_is_safe(board, unit, possible_goal[0]):
            return possible_goal[0]

    return None

def check_for_freeze_powerup_goal(board, unit):
    for cell in board.cells:
        if cell.freeze_powerup and not cell.unit:
            min_unit_id, min_safe_dist = None, UNREACHABLE
            for unit_id in board.units:
                if board.units[unit_id].hp <= 0:
                    continue
                safe_dist = cell.safe_dists[unit_id][0]
                if safe_dist < min_safe_dist:
                    min_unit_id, min_safe_dist = unit_id, safe_dist
            #print(f'FREEZE: unit {min_unit_id} is {min_safe_dist} ticks away')
            #if unit.player is board.player['b']:
            #    print(f'unit {unit.id} dist={cell.safe_dists[unit.id][0]}')
            if board.tick + min_safe_dist < cell.expires and min_unit_id == unit.id:
                if goal_is_safe(board, unit, cell):
                    return cell
    return None

def check_for_blast_powerup_goal(board, unit):
    for cell in board.cells:
        if cell.blast_powerup and not cell.unit:
            min_unit_id, min_safe_dist = None, UNREACHABLE
            for unit_id in board.units:
                if board.units[unit_id].hp <= 0:
                    continue
                safe_dist = cell.safe_dists[unit_id][0]
                if safe_dist < min_safe_dist:
                    min_unit_id, min_safe_dist = unit_id, safe_dist
            #print(f'BLAST: unit {min_unit_id} is {min_safe_dist} ticks away')
            if board.tick + min_safe_dist < cell.expires and min_unit_id == unit.id:
                if goal_is_safe(board, unit, cell):
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
            if goal_is_safe(board, unit, chok_cell):
                return choke_cell
    return None

def can_bomb_for_opp_disruption(board, unit):
    if unit.cell.bomb_diameter or len(unit.player.bombs) == 3:
        return False
    if (unit.cell.fire and unit.cell.expires > board.tick + 1 # still on fire next turn
        and unit.invulnerable < board.tick + 3):  # Invulnerable in < 3 turns (explosion + 2 escape moves)
        return False
    if not can_bomb_safely(board, unit):
        return False

    blast_cells = board.get_bomb_area(unit.cell, diameter=unit.diameter)
    opp_id = 'a' if unit.player.id == 'b' else 'b'
    opp_units = [u for u in board.players[opp_id].units if u.hp > 0]

    for opp_unit in opp_units:
        safe_cell_set1, safe_cell_set2 = set(), set()
        truly_safe_cell_set1, truly_safe_cell_set2 = set(), set()
        max_safe_dist1, max_safe_dist2 = 0, 0
        for dist in range(1, len(opp_unit.cell.safe_paths)):
            safe_cells = opp_unit.cell.safe_paths[dist]
            for safe_cell in safe_cells:
                max_safe_dist1 = dist
                safe_cell_set1.add(safe_cell)
                if not safe_cell.future_fire_start:
                    truly_safe_cell_set1.add(safe_cell)
                if not safe_cell in blast_cells: # If the safe cell is unaffected 
                    max_safe_dist2 = dist
                    safe_cell_set2.add(safe_cell)
                    if not safe_cell.future_fire_start:
                        truly_safe_cell_set2.add(safe_cell)
        if (max_safe_dist2 <= max_safe_dist1 / 2
            or len(safe_cell_set2) <= len(safe_cell_set1) / 2
            or len(truly_safe_cell_set2) <= len(truly_safe_cell_set1) / 2):
            return True
    return False

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
    target_range_lim = 0.02 if board.tick < 200 else 0.01

    #t = time.time()
    possible_goals = []
    for cell in board.cells:
        if cell.bomb_diameter or cell.wall or unit.player.id in cell.future_fire_start:
            continue
        target_range_i = min(len(cell.target_range) - 1, ((unit.diameter // 2) - 1))
        target_range = cell.target_range[target_range_i]
        target_range = target_range if (unit.player.id == 'a') else -target_range
        if target_range >= target_range_lim:  # at least 2 ore boxes typically
            safe_dist = cell.safe_dists[unit.id][0]
            if safe_dist != UNREACHABLE:
                possible_goals.append((cell, safe_dist, target_range))
    if possible_goals:
        possible_goals.sort(key=lambda x: x[2] / (x[1] + 6), reverse=True)
        for possible_goal in possible_goals:
            if goal_is_safe(board, unit, possible_goal[0]):
                #print(f'CHECK_FOR_MINING TIME1 {round((time.time() - t) * 1000, 3)}')
                return possible_goal[0]
    #print(f'CHECK_FOR_MINING TIME2 {round((time.time() - t) * 1000, 3)}')
    return None

def check_for_any_safe_goal(board, unit):
    for dist in range(len(unit.cell.safe_paths) - 1, 0, -1): # backwards, don't check dist==0
        for safe_cell in unit.cell.safe_paths[dist]:
            if not safe_cell.future_fire_start:
                return safe_cell # Return furthest truly safe destination
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
    print(f'unit {unit.id} do_move tick{board.tick} {dest_cell.x},{dest_cell.y} via {move_cell.x},{move_cell.y} ({action})')
    if action:
        await board._client.send_move(action, unit.id)
        #asyncio.ensure_future(board._client.send_move(action, unit.id))
        #start_time = time.time()
        #if (board.tick < 200 and 1000 * (time.time() - board.tick_start) < 60):
        #or (board.tick >= 200 and 1000 * (time.time() - board.tick_start) < 40)):
        if (1000 * (time.time() - board.tick_start) < 60):
            board._update_dists()
            board._update_target_range() # need dists first

async def do_bomb(board, unit):
    # Update future_fire_start for unit's player
    blast_cells = board.get_bomb_area(unit.cell, diameter=unit.diameter)
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
    print(f'unit {unit.id} do_bomb tick{board.tick} {unit.cell.x},{unit.cell.y}')
    await board._client.send_bomb(unit.id)
    #asyncio.ensure_future(board._client.send_bomb(unit.id))
    #if (board.tick < 200 and 1000 * (time.time() - board.tick_start) < 60):
    #or (board.tick >= 200 and 1000 * (time.time() - board.tick_start) < 40)):
    if (1000 * (time.time() - board.tick_start) < 60):
        board._update_dists()
        board._update_target_range() # need dists first

async def do_detonate(board, unit, bomb_cell):
    # TODO get blast_cells and set future_fire for board.player.id
    #blast_cells = board.get_bomb_area(unit.cell, unit.diameter)
    blast_cells = board.get_bomb_area(bomb_cell)
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
    print(f'unit {unit.id} do_detonate tick{board.tick} {bomb_cell.x},{bomb_cell.y}')
    await board._client.send_detonate(bomb_cell.x, bomb_cell.y, unit.id)
    #asyncio.ensure_future(board._client.send_detonate(bomb_cell.x, bomb_cell.y, unit.id))
    #if (board.tick < 200 and 1000 * (time.time() - board.tick_start) < 60):
    #or (board.tick >= 200 and 1000 * (time.time() - board.tick_start) < 40)):
    if (1000 * (time.time() - board.tick_start) < 60):
        board._update_dists()
        board._update_target_range() # need dists first

async def act(board, units): # TODORMA
    start_time = time.time()
    #print('A', 0, round((time.time() - start_time) * 1000))
    
    # TODO goal of move to center if near outside late in game (after 200)
    for player in board.players.values():
        player.save_bombs = list(player.bombs)
    for unit in board.units.values():
        unit.save_bombs = list(unit.bombs)
    for cell in board.cells:
        cell.save_bomb_diameter = cell.bomb_diameter
        cell.save_bomb_unit = cell.bomb_unit

    units_done = []

    #print('B', len(units_done), round((time.time() - start_time) * 1000))
    for unit in [u for u in units if u not in units_done]:
        flee_danger_goal = check_for_flee_danger_goal(board, unit)
        if flee_danger_goal:
            print(f'unit {unit.id} flee danger')
            await do_move(board, unit, flee_danger_goal)
            units_done.append(unit)

    #print('C', len(units_done), round((time.time() - start_time) * 1000))
    for unit in [u for u in units if u not in units_done]:
        for bomb_cell in unit.bombs:
            if can_detonate_for_damage(board, unit, bomb_cell):
                print(f'unit {unit.id} detonate for damage')
                await do_detonate(board, unit, bomb_cell)
                units_done.append(unit)
                break # out of bomb loop
    # Detonation safety if a bomb threatens a stunned opp
    # Just detonation safety in general?
    # TODORMA
    #for unit in [u for u in units if u not in units_done]:
    #    detonation_safety_goal = check_for_detonation_safety_goal(board, unit)
    #    if detonation_safety_goal:
    #        print(f'unit {unit.id} goal: detonation safety {detonation_safety_goal.x},{detonation_safety_goal.y}')
    #        await do_move(board, unit, detonation_safety_goal)
    #        units_done.append(unit)

    #print('D', len(units_done), round((time.time() - start_time) * 1000))
    for unit in [u for u in units if u not in units_done]:
        if can_bomb_for_stunned_opp(board, unit):
            print(f'unit {unit.id} bomb for stunned opp')
            await do_bomb(board, unit)
            units_done.append(unit)

    #print('E', len(units_done), round((time.time() - start_time) * 1000))
    for unit in [u for u in units if u not in units_done]:
        if can_bomb_for_instant_damage(board, unit):
            print(f'unit {unit.id} bomb for instant damage')
            await do_bomb(board, unit)
            units_done.append(unit)

    #print('F', len(units_done), round((time.time() - start_time) * 1000))
    for unit in [u for u in units if u not in units_done]:
        stun_attack_goal = check_for_stun_attack_goal(board, unit)
        if stun_attack_goal:
            print(f'unit {unit.id} goal: stun attack {stun_attack_goal.x},{stun_attack_goal.y}')
            await do_move(board, unit, stun_attack_goal)
            units_done.append(unit)

    #print('G', len(units_done), round((time.time() - start_time) * 1000))
    for unit in [u for u in units if u not in units_done]:
        freeze_powerup_goal = check_for_freeze_powerup_goal(board, unit)
        if freeze_powerup_goal:
            print(f'unit {unit.id} goal: freeze powerup {freeze_powerup_goal.x},{freeze_powerup_goal.y}')
            await do_move(board, unit, freeze_powerup_goal)
            units_done.append(unit)

    #print('H', len(units_done), round((time.time() - start_time) * 1000))
    for unit in [u for u in units if u not in units_done]:
        blast_powerup_goal = check_for_blast_powerup_goal(board, unit)
        if blast_powerup_goal:
            print(f'unit {unit.id} goal: blast powerup {blast_powerup_goal.x},{blast_powerup_goal.y}')
            await do_move(board, unit, blast_powerup_goal)
            units_done.append(unit)

    #print('I', len(units_done), round((time.time() - start_time) * 1000))
    for unit in [u for u in units if u not in units_done]:
        detonation_safety_goal = check_for_detonation_safety_goal(board, unit)
        if detonation_safety_goal:
            print(f'unit {unit.id} goal: detonation safety {detonation_safety_goal.x},{detonation_safety_goal.y}')
            await do_move(board, unit, detonation_safety_goal)
            units_done.append(unit)

    # TODO RMA
    # approach opp with limited safe range?
            
    #print('J', len(units_done), round((time.time() - start_time) * 1000))
    for unit in [u for u in units if u not in units_done]:
        choke_point_goal = check_for_choke_point_goal(board, unit)
        if choke_point_goal:
            print(f'unit {unit.id} goal: choke point {choke_point_goal.x},{choke_point_goal.y}')
            await do_move(board, unit, choke_point_goal)
            units_done.append(unit)

    #print('K', len(units_done), round((time.time() - start_time) * 1000))
    for unit in [u for u in units if u not in units_done]:
        if can_bomb_for_opp_disruption(board, unit):
            print(f'unit {unit.id} bomb for opp disruption')
            await do_bomb(board, unit)
            units_done.append(unit)

    #print('L', len(units_done), round((time.time() - start_time) * 1000))
    for unit in [u for u in units if u not in units_done]:
        for bomb_cell in unit.bombs:
            assert bomb_cell.bomb_diameter
            if can_detonate_for_no_damage(board, unit, bomb_cell):
                print(f'unit {unit.id} detonate for no damage')
                await do_detonate(board, unit, bomb_cell)
                units_done.append(unit)
                break # out of bomb loop

    #print('M', len(units_done), round((time.time() - start_time) * 1000))
    for unit in [u for u in units if u not in units_done]:
        if can_bomb_for_mining(board, unit):
            print(f'unit {unit.id} bomb for mining')
            await do_bomb(board, unit)
            units_done.append(unit)

    #print('N', len(units_done), round((time.time() - start_time) * 1000))
    for unit in [u for u in units if u not in units_done]:
        mining_goal = check_for_mining_goal(board, unit)
        if mining_goal:
            print(f'unit {unit.id} goal: mining {mining_goal.x},{mining_goal.y}')
            print('N1', len(units_done), round((time.time() - start_time) * 1000))
            await do_move(board, unit, mining_goal)
            #asyncio.ensure_future(do_move(board, unit, mining_goal))
            print('N2', len(units_done), round((time.time() - start_time) * 1000))
            units_done.append(unit)

    #print('O', len(units_done), round((time.time() - start_time) * 1000))
    for unit in [u for u in units if u not in units_done]:
        any_safe_goal = check_for_any_safe_goal(board, unit)
        if any_safe_goal:
            print(f'unit {unit.id} goal: anything safe {any_safe_goal.x},{any_safe_goal.y}')
            await do_move(board, unit, any_safe_goal)
            units_done.append(units)

    #print('P', len(units_done), round((time.time() - start_time) * 1000))
    assert len(units) == len(units_done)

    # Restore original board settings after all units have been processed
    for player in board.players.values():
        player.bombs = list(player.save_bombs)
    for unit in board.units.values():
        unit.bombs = list(unit.save_bombs)
    for cell in board.cells:
        cell.bomb_diameter = cell.save_bomb_diameter
        cell.bomb_unit = cell.save_bomb_unit
    #print('Q', len(units_done), round((time.time() - start_time) * 1000))


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
        board.start_time = time.time()
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
