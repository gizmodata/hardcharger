import type { Plugin } from "vite";

export const allowedLicenses: Set<string>;
export const licenseExceptions: Map<string, { license: string; reason: string }>;
export function licenseAllowed(expression: string, allowed?: Set<string>): boolean;
export function declaredLicense(pkg: Record<string, unknown>): string | undefined;
export function packageDirOf(id: string, root: string): string | undefined;
export function buildNotices(
  found: Map<string, Set<string>>,
  options: { title: string },
): { problems: string[]; flagged: string[]; text?: string; packages?: string[] };
export function thirdPartyNotices(options: { root: string; fileName?: string; title: string | (() => string) }): {
  plugin: Plugin;
  workerPlugin: Plugin;
};
