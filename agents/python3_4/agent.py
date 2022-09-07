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

from game_state import GameState
import asyncio
import random
import os
import time

uri = os.environ.get(
    'GAME_CONNECTION_STRING') or "ws://127.0.0.1:3000/?role=agent&agentId=agentId&name=defaultName"


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
    init_cell = unit.cell
    #init_center_dist = abs(init_cell.x - 7) + abs(init_cell.y - 7)
    #center_dist = abs(move_cell.x - 7) + abs(move_cell.y - 7)
    init_goal_dist = unit.goal_cell.dists[unit.id][0]
    goal_dist = init_goal_dist if move_cell is init_cell else move_cell.get_dist(unit.goal_cell, unit.player)

    move_score = 0
    if move_cell.blast_powerup or move_cell.freeze_powerup:
        move_score += 100
    if goal_dist < init_goal_dist:
        move_score += 10
    if goal_dist > init_goal_dist:
        move_score -= 10
    #if center_dist > init_center_dist:
    #    move_score += 1
    # TODO: Analyze couple steps of path to determine safety
    # It is currently 2
    # Tick+1 is 3
    # Fire can start at 4
    if move_cell.future_fire_start:
        print(f'Unit {unit.id} move to {move_cell.x},{move_cell.y}: start/tick+1/end: {move_cell.future_fire_start} <= {board.tick + 1} < {move_cell.future_fire_end}')
    if move_cell.future_fire_start and (move_cell.future_fire_start <= board.tick + 1 < move_cell.future_fire_end):
        move_score -= 1000
    if (move_cell.fire
        and move_cell.expires > board.tick + 1    # Still on fire next turn
        and unit.invulnerable < board.tick + 1):  # Vulnerable next turn
        move_score -= 10000
    return move_score


def get_bomb_score(board, unit):
    if len(unit.bombs) == 3:
        return -1
    if unit.cell.bomb_diameter:
        return -1
    if unit.cell is unit.goal_cell: # TODO Should not always do this. Other goals exist.
        return 0.5
    if (unit.cell.fire
        and unit.cell.expires > board.tick + 1     # Still on fire next turn
        and unit.invulnerable >= board.tick + 3):  # Invulnerable in 3 turns (explosion + 2 escape moves)
        instant_detonate_score = get_detonate_score(board, unit, unit.cell)
        if instant_detonate_score > 1:
            return instant_detonate_score
    return 0


def get_detonate_score(board, unit, bomb_cell):
    if bomb_cell.created and board.tick < bomb_cell.created + 5:
        return 0

    # TODO: debug scoring of blast cells and verify accuracy of get_bomb_area()
    detonate_score = 0
    blast_cells = board.get_bomb_area(bomb_cell)
    for blast_cell in blast_cells:
        if blast_cell.unit and blast_cell.unit.invulnerable < board.tick + 1:
            ds = 0
            if blast_cell.unit.hp > 1:
                ds = 10
            elif blast_cell.unit.hp == 1:
                ds = 15
            detonate_score += -ds if blast_cell.unit.player is unit.player else ds
        if blast_cell.box:
            detonate_score += 1 / (10 ** (blast_cell.hp - 2)) # 0.1, 0.01, 0.001
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
        assign_goals(board, board.player.units)
        for unit in board.player.units:
            # MOVE SCORES
            move_scores = []
            for move_cell in unit.cell.move_neighbors():
                move_score = get_move_score(board, unit, move_cell)
                move_scores.append((move_cell, move_score))
                #print(f'Unit {unit.id} move {cell.x},{cell.y} to {move_cell.x},{move_cell.y} dist change from {init_goal_dist} to {goal_dist}. move_score={move_score} (goal at {unit.goal_cell.x},{unit.goal_cell.y})')

            no_move_cell, no_move_score = unit.cell, get_move_score(board, unit, unit.cell)
            move_scores.append((no_move_cell, no_move_score))
            move_scores.sort(key=lambda x: x[1], reverse=True)
            best_move_cell, best_move_score = move_scores[0]

            # BOMB SCORES
            bomb_score = get_bomb_score(board, unit)

            # DETONATE SCORES
            detonate_scores = []
            for bomb_cell in unit.bombs:
                detonate_score = get_detonate_score(board, unit, bomb_cell)
                detonate_scores.append((bomb_cell, detonate_score))
            detonate_scores.sort(key=lambda x: x[1], reverse=True)
            best_detonate_cell, best_detonate_score = None, -1
            if detonate_scores:
                best_detonate_cell, best_detonate_score = detonate_scores[0]

            # LOGGING AND DECISION MAKING
            ACTIONS = {
                unit.cell: None,
                unit.cell.west:  'left',
                unit.cell.east:  'right',
                unit.cell.north: 'up',
                unit.cell.south: 'down',
            }

            for i, (move_cell, move_score) in enumerate(move_scores):
                prefix = '!' if i == 0 else ''
                print(f'{prefix}Unit {unit.id} move {ACTIONS[move_cell]}: score={move_score}')

            if bomb_score > 0:
                print(f'Unit {unit.id} bomb score={bomb_score}')

            if best_detonate_score > 0:
                c = best_detonate_cell
                print(f'Unit {unit.id} detonate score={best_detonate_score} at {c.x},{c.y}')

            if best_detonate_score > 0:
                c = best_detonate_cell
                await self._client.send_detonate(c.x, c.y, unit.id)
            elif bomb_score > 0:
                await self._client.send_bomb(unit.id)
            else:
                action = ACTIONS[best_move_cell]
                if action:
                    await self._client.send_move(action, unit.id)


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
