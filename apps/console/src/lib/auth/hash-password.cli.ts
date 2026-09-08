import { stdin } from "node:process";

import { hashConsolePassword } from "./credentials";

const argPassword = process.argv[2];
const stdinPassword = stdin.isTTY ? "" : await readStdin();
const password = argPassword ?? stdinPassword.replace(/\r?\n$/, "");

if (!password) {
  console.error("Usage: printf '%s' 'password' | pnpm --filter @hexera/console auth:hash");
  process.exit(1);
}

console.log(await hashConsolePassword(password));

function readStdin(): Promise<string> {
  return new Promise((resolve, reject) => {
    let value = "";
    stdin.setEncoding("utf8");
    stdin.on("data", (chunk: string) => {
      value += chunk;
    });
    stdin.on("end", () => resolve(value));
    stdin.on("error", reject);
  });
}
