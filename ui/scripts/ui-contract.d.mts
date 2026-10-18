export const contentSecurityPolicy: string;
export const securityHeaders: Record<string, string>;
export function cacheControl(path: string): string;
export const contentTypes: Record<string, string>;
export function resolveUiPath(rest: string, exists: (path: string) => boolean): string | undefined;
