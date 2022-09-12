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
    detonate_score = 0
    blast_cells = board.get_bomb_area(bomb_cell, diameter=diameter)
    for blast_cell in blast_cells:
        if blast_cell.unit and blast_cell.unit.invulnerable < board.tick + 1:
            ds = 0
            if blast_cell.unit.hp == 3:
                ds = 10
            elif blast_cell.unit.hp == 2:
                ds = 11
            elif blast_cell.unit.hp == 1:
                ds = 20
            detonate_score += (-ds if (blast_cell.unit.player is unit.player) else ds)
    return detonate_score > 0

def can_bomb_for_stunned_opp(board, unit):
    if unit.cell.bomb_diameter or len(unit.player.bombs) == 3:
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

def check_for_powerup_goal(board, unit):
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
    detonate_score = 0
    blast_cells = board.get_bomb_area(bomb_cell)
    for blast_cell in blast_cells:
        if blast_cell.unit and blast_cell.unit.invulnerable < board.tick + 1:
            ds = 0
            if blast_cell.unit.hp == 3:
                ds = 10
            elif blast_cell.unit.hp == 2:
                ds = 11
            elif blast_cell.unit.hp == 1:
                ds = 20
            detonate_score += (-ds if (blast_cell.unit.player is unit.player) else ds)
    return detonate_score == 0

def check_for_mining_goal(board, unit):
    possible_goals = []
    for cell in board.cells:
        if cell.bomb_diameter:
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
    mining_goal = check_for_mining_goal(board, unit)
    return mining_goal is unit.cell

async def do_move(board, unit, dest_cell):
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
    print(f'unit {unit.id} do_move {dest_cell.x},{dest_cell.y} via {move_cell.x},{move_cell.y} ({action})')
    if action:
        await board._client.send_move(action, unit.id)

async def do_bomb(board, unit):
    print(f'unit {unit.id} do_bomb {unit.cell.x},{unit.cell.y}')
    await board._client.send_bomb(unit.id)

async def do_detonate(board, unit, bomb_cell):
    print(f'unit {unit.id} do_detonate {bomb_cell.x},{bomb_cell.y}')
    await board._client.send_detonate(bomb_cell.x, bomb_cell.y, unit.id)

async def act(board, unit): # TODORMA
    flee_danger_goal = check_for_flee_danger_goal(board, unit)
    if flee_danger_goal:
        print(f'unit {unit.id} flee danger')
        return await do_move(board, unit, flee_danger_goal)
    for bomb_cell in unit.bombs:
        if can_detonate_for_damage(board, unit, bomb_cell):
            print(f'unit {unit.id} detonate for damage')
            return await do_detonate(board, unit, bomb_cell)
    if can_bomb_for_stunned_opp(board, unit):
        print(f'unit {unit.id} bomb for stunned opp')
        return await do_bomb(board, unit)
    if can_bomb_for_instant_damage(board, unit):
        print(f'unit {unit.id} bomb for instant damage')
        return await do_bomb(board, unit)
    stun_attack_goal = check_for_stun_attack_goal(board, unit)
    if stun_attack_goal:
        print(f'unit {unit.id} goal: stun attack {stun_attack_goal.x},{stun_attack_goal.y}')
        return await do_move(board, unit, stun_attack_goal)
    powerup_goal = check_for_powerup_goal(board, unit)
    if powerup_goal:
        print(f'unit {unit.id} goal: powerup {powerup_goal.x},{powerup_goal.y}')
        return await do_move(board, unit, powerup_goal)
    detonation_safety_goal = check_for_detonation_safety_goal(board, unit)
    if detonation_safety_goal:
        print(f'unit {unit.id} goal: detonation safety {detonation_safety_goal.x},{detonation_safety_goal.y}')
        return await do_move(board, unit, detonation_safety_goal)
    choke_point_goal = check_for_choke_point_goal(board, unit)
    if choke_point_goal:
        print(f'unit {unit.id} goal: choke point {choke_point_goal.x},{choke_point_goal.y}')
        return await do_move(board, unit, choke_point_goal)
    for bomb_cell in unit.bombs:
        if can_detonate_for_no_damage(board, unit, bomb_cell):
            print(f'unit {unit.id} detonate for no damage')
            return await do_detonate(board, unit, bomb_cell)
    if can_bomb_for_mining(board, unit):
        print(f'unit {unit.id} bomb for mining')
        return await do_bomb(board, unit)
    mining_goal = check_for_mining_goal(board, unit)
    if mining_goal:
        print(f'unit {unit.id} goal: mining {mining_goal.x},{mining_goal.y}')
        return await do_move(board, unit, mining_goal)
    any_safe_goal = check_for_any_safe_goal(board, unit)
    if any_safe_goal:
        print(f'unit {unit.id} goal: anything safe {any_safe_goal.x},{any_safe_goal.y}')
        return await do_move(board, unit, any_safe_goal)
    assert False


#################################################################



def assign_goals(board, units):
    units = list(units)
    goal_cells = []
    unit_goal_lists = []
    for unit in units:
        unit.set_goal_list()
    while units:
        max_score, max_score_cell, max_score_unit = -10000, None, None
        for unit in units:
            for score, cell in unit.goal_list:
                if score > max_score and not cell in goal_cells:
                    max_score, max_score_cell, max_score_unit = score, cell, unit
                    break # Best goal possible for this unit, break out to go to next unit list
        if max_score_unit is None:
            print(unit.goal_list)
        print(f'Unit {max_score_unit.id} goal {max_score_cell.x},{max_score_cell.y}: score={max_score}, dist={max_score_cell.dists[unit.id]}')
        max_score_unit.goal_cell = max_score_cell
        goal_cells.append(max_score_cell)
        units.remove(max_score_unit)


def get_move_score(board, unit, move_cell):
    own_id, opp_id = unit.player.id, ('a' if unit.player.id == 'b' else 'b')
    init_cell = unit.cell
    init_center_dist = abs(init_cell.x - 7) + abs(init_cell.y - 7)
    center_dist = abs(move_cell.x - 7) + abs(move_cell.y - 7)
    init_goal_dist = unit.goal_cell.dists[unit.id][0]
    goal_dist = init_goal_dist if move_cell is init_cell else move_cell.get_dist(unit.goal_cell, unit.player)

    move_score = 0
    if move_cell.blast_powerup or move_cell.freeze_powerup:
        move_score += 100
    if goal_dist < init_goal_dist:
        move_score += 10
    if goal_dist > init_goal_dist:
        move_score -= 10
    if center_dist < init_center_dist:
        move_score += 1

    # TODO: Analyze couple steps of path to determine safety
    arrival_tick = board.tick + 1
    if opp_id in move_cell.future_fire_start:
        print(f'Unit {unit.id} move to {move_cell.x},{move_cell.y}: OPP start/tick+1/end: {move_cell.future_fire_start[opp_id]} <= {board.tick + 2} < {move_cell.future_fire_end[opp_id]}')
    if own_id in move_cell.future_fire_start:
        print(f'Unit {unit.id} move to {move_cell.x},{move_cell.y}: OWN start/tick+1/end: {move_cell.future_fire_start[own_id]} <= {board.tick + 2} < {move_cell.future_fire_end[own_id]}')

    if (opp_id in move_cell.future_fire_start
        # Possible opponent attack
        # fire_start = 5
        #   tick = 2, arrive = 3 (Ok, can leave on 4 safely)
        #   tick = 3, arrive = 4 (Bad, can't leave in time)
        #   tick = 4, arrive = 5 (Bad, arrive on fire turn)
        and move_cell.future_fire_start[opp_id] <= arrival_tick + 1):
        move_score -= 1000
    if (own_id in move_cell.future_fire_end
        # Forced friendly fire
        and move_cell.future_fire_end[own_id] - 5 <= arrival_tick + 1):
        move_score -= 5000
    if (move_cell.fire
        and arrival_tick + 1 < move_cell.expires    # need to survive 2+ ticks at dest (arrive tick and depart tick)
        and unit.invulnerable < arrival_tick + 1):  # Vulnerable in 2 turns
        move_score -= 10000
    if (move_cell.fire
        # You don't want to step onto a fire square, then realize the fire lasts 1+ turns and your invuln lasts 0 more
        # You'll get burned before you move off
        #
        # If fire lasts 1 more turn,  need 1+ invuln
        # If fire lasts 2 more turns, need 2+ invuln
        # If fire lasts 3 more turns, need 2+ invuln
        and arrival_tick + 1 == move_cell.expires   # only need to survive next tick (arrive tick)
        and unit.invulnerable < arrival_tick):  # Vulnerable next turn
        move_score -= 20000

    return move_score


def get_bomb_score(board, unit):
    if len(unit.player.bombs) == 3:
        return -1
    if unit.cell.bomb_diameter:
        return -1
    if unit.cell is unit.goal_cell: # TODO Should not always do this. Other goals exist.
        if unit.bombs: # If one bomb has already out from this unit, this second one better be good
            # different types of cell scores? this is the attack score?
            target_range_i = min(len(unit.cell.target_range) - 1, ((unit.diameter // 2) - 1))
            target_range = unit.cell.target_range[target_range_i]
            target_range = target_range if (unit.player.id == 'a') else -target_range
            if target_range > 5:
                return 0.6
        else:
            return 0.5
    if (unit.cell.fire
        and unit.cell.expires > board.tick + 1     # Still on fire next turn
        and unit.invulnerable >= board.tick + 3):  # Invulnerable in 3 turns (explosion + 2 escape moves)
        instant_detonate_score = get_detonate_score(board, unit, unit.cell, diameter=unit.diameter)
        if instant_detonate_score > 1:
            print('!!!Instant detonate!!!')
            return instant_detonate_score
    return 0


def get_detonate_score(board, unit, bomb_cell, diameter=None):
    if bomb_cell.created and board.tick < bomb_cell.created + 5:
        return 0

    detonate_score = 0
    blast_cells = board.get_bomb_area(bomb_cell, diameter=diameter)

    for blast_cell in blast_cells:
        if blast_cell.unit and blast_cell.unit.invulnerable < board.tick + 1:
            ds = 0
            if blast_cell.unit.hp > 1:
                ds = 10
            elif blast_cell.unit.hp == 1:
                ds = 20
            detonate_score += (-ds if (blast_cell.unit.player is unit.player) else ds)
            print(f'Blast cell {blast_cell.x},{blast_cell.y}: unit {blast_cell.unit.id}, player {blast_cell.unit.player.id}, current player {unit.player.id}, same? {blast_cell.unit.player is unit.player}, hp {blast_cell.unit.hp}, score {detonate_score}')
        if blast_cell.box:
            detonate_score += 1 / (10 ** blast_cell.hp) # 0.1, 0.01, 0.001
    return detonate_score


class Agent():
    def __init__(self):
        self._client = GameState(uri)
        self._client.set_game_tick_callback(self._on_game_tick)
        loop = asyncio.get_event_loop()
        connection = loop.run_until_complete(self._client.connect())
        tasks = [asyncio.ensure_future(self._client._handle_messages(connection))]
        loop.run_until_complete(asyncio.wait(tasks))

    async def _on_game_tick(self, board):
        for unit in board.player.units:
            if unit.hp <= 0:
                continue
            await act(board, unit)
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
