import type { Server } from "node:http";

export function createUiServer(options: { dist: string; upstream: string }): Server;
