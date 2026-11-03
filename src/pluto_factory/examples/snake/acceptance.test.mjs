import assert from "node:assert/strict";
import path from "node:path";
import test from "node:test";
import { pathToFileURL } from "node:url";

const game = await import(pathToFileURL(path.join(process.cwd(), "game.mjs")));
const { newGame, turn, tick, togglePause, restart } = game;

test("the engine exposes its pure state operations", () => {
  for (const operation of [newGame, turn, tick, togglePause, restart]) {
    assert.equal(typeof operation, "function");
  }
});

test("new games have deterministic, valid initial state", () => {
  const state = newGame({ width: 12, height: 12, seed: 42 });
  assert.equal(state.width, 12);
  assert.equal(state.height, 12);
  assert.equal(state.score, 0);
  assert.equal(state.status, "running");
  assert.equal(state.direction, "right");
  assert.ok(state.snake.length >= 3);
  assert.ok(state.food.x >= 0 && state.food.x < 12);
  assert.ok(state.food.y >= 0 && state.food.y < 12);
  assert.ok(!state.snake.some(cell => cell.x === state.food.x && cell.y === state.food.y));
  assert.deepEqual(state, newGame({ width: 12, height: 12, seed: 42 }));
});

test("ticks move one cell and do not mutate their input", () => {
  const state = { ...newGame({ width: 12, height: 12 }), food: { x: 0, y: 0 } };
  const original = structuredClone(state);
  const next = tick(state);
  assert.deepEqual(state, original);
  assert.equal(next.snake[0].x, state.snake[0].x + 1);
  assert.equal(next.snake[0].y, state.snake[0].y);
  assert.equal(next.snake.length, state.snake.length);
});

test("turns apply at the next tick and direct reversal is ignored", () => {
  const state = { ...newGame({ width: 12, height: 12 }), food: { x: 0, y: 0 } };
  const reversed = tick(turn(state, "left"));
  assert.equal(reversed.snake[0].x, state.snake[0].x + 1);
  const upward = tick(turn(state, "up"));
  assert.equal(upward.snake[0].x, state.snake[0].x);
  assert.equal(upward.snake[0].y, state.snake[0].y - 1);
});

test("eating grows the snake, scores, and places food in a free cell", () => {
  const state = newGame({ width: 12, height: 12, seed: 8 });
  state.food = { x: state.snake[0].x + 1, y: state.snake[0].y };
  const next = tick(state);
  assert.equal(next.score, 1);
  assert.equal(next.snake.length, state.snake.length + 1);
  assert.ok(!next.snake.some(cell => cell.x === next.food.x && cell.y === next.food.y));
});

test("wall and self collisions end the game", () => {
  const wall = {
    ...newGame({ width: 12, height: 12 }),
    snake: [{ x: 11, y: 6 }, { x: 10, y: 6 }, { x: 9, y: 6 }],
    direction: "right", food: { x: 0, y: 0 },
  };
  assert.equal(tick(wall).status, "gameover");
  const self = {
    ...wall, direction: "up",
    snake: [
      { x: 2, y: 2 }, { x: 1, y: 2 }, { x: 1, y: 1 },
      { x: 2, y: 1 }, { x: 3, y: 1 },
    ],
  };
  assert.equal(tick(self).status, "gameover");
});

test("paused games do not advance, and restart resets progress", () => {
  const state = newGame({ width: 12, height: 12, seed: 7 });
  const paused = togglePause(state);
  assert.equal(paused.status, "paused");
  assert.deepEqual(tick(paused), paused);
  assert.equal(togglePause(paused).status, "running");
  const fresh = restart({ ...state, score: 19, status: "gameover" });
  assert.equal(fresh.score, 0);
  assert.equal(fresh.status, "running");
  assert.equal(fresh.width, 12);
  assert.equal(fresh.height, 12);
});
