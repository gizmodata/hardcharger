import { startHarness, stateVariable } from "./harness.ts";

export default async function globalSetup(): Promise<() => Promise<void>> {
  const harness = await startHarness();
  process.env[stateVariable] = JSON.stringify(harness.state);
  return harness.stop;
}
