import { render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import { App } from "./App";

afterEach(() => vi.unstubAllGlobals());

test("shows the API's status", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify({ status: "ok" }), { status: 200 })),
  );
  render(<App />);
  expect(await screen.findByText("API: ok")).toBeTruthy();
});

test("says when the API can't be reached", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response("", { status: 500 })),
  );
  render(<App />);
  expect(await screen.findByText("API: unreachable")).toBeTruthy();
});
