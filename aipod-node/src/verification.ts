import { spawn } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

import { redact } from "./traces.js";

export interface CommandEvidence {
  status: "passed" | "failed" | "timeout";
  command: string[];
  exitCode: number | null;
  durationMs: number;
  stdout: string;
  stderr: string;
}

const bounded = (value: string, limit = 20_000) =>
  value.length <= limit ? value : `${value.slice(0, limit)}\n… [truncated]`;

export async function runVerificationCommand(
  projectRoot: string,
  command: string[],
  timeoutMs = 120_000,
): Promise<CommandEvidence> {
  if (!command.length) throw new Error("Verification command is empty");
  const started = performance.now();
  return new Promise((resolveEvidence, reject) => {
    const child = spawn(command[0]!, command.slice(1), {
      cwd: projectRoot,
      shell: false,
      env: { ...process.env, AIPOD_PROJECT_ROOT: resolve(projectRoot) },
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => { stdout = bounded(stdout + String(chunk)); });
    child.stderr.on("data", (chunk) => { stderr = bounded(stderr + String(chunk)); });
    let timedOut = false;
    const timeout = setTimeout(() => {
      timedOut = true;
      child.kill("SIGTERM");
    }, timeoutMs);
    child.once("error", (error) => {
      clearTimeout(timeout);
      reject(error);
    });
    child.once("close", async (exitCode) => {
      clearTimeout(timeout);
      const evidence = redact({
        status: timedOut ? "timeout" : exitCode === 0 ? "passed" : "failed",
        command,
        exitCode,
        durationMs: performance.now() - started,
        stdout,
        stderr,
      }) as unknown as CommandEvidence;
      const directory = resolve(projectRoot, ".aipod");
      await mkdir(directory, { recursive: true });
      await writeFile(
        resolve(directory, "verification.json"),
        `${JSON.stringify(evidence, null, 2)}\n`,
      );
      resolveEvidence(evidence);
    });
  });
}
