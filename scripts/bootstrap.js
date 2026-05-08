import { spawn } from "child_process";
import fs from "fs";
import path from "path";

const ROOT = process.cwd();
const ENV_PATH = path.join(ROOT, ".env");
const TRADERS_PATH = path.join(ROOT, "traders.json");

const BOT_KEYS = [
  "BOT_NOISE_PK",
  "BOT_SHOCK_PK",
  "BOT_TREND_PK",
  "BOT_MEAN_REVERSION_PK",
  "EXT_BOT_0_PK",
];

const BOT_NAMES = [
  "Bot de Ruído",
  "Bot de Choque",
  "Bot de Tendência",
  "Bot de Reversão à Média",
  "Bot28867",
];

function parseEnv(content) {
  const env = {};

  for (const line of content.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;

    const idx = trimmed.indexOf("=");
    if (idx === -1) continue;

    env[trimmed.slice(0, idx).trim()] = trimmed.slice(idx + 1).trim();
  }

  return env;
}


function buildEnv(env) {
  return Object.entries(env)
    .map(([key, value]) => `${key}=${value}`)
    .join("\n") + "\n";
}

function writeEnv(newValues) {
  let env = {};

  if (fs.existsSync(ENV_PATH)) {
    env = parseEnv(fs.readFileSync(ENV_PATH, "utf-8"));
  }

  env = {
    ...env,
    ...newValues,
  };

  fs.writeFileSync(ENV_PATH, buildEnv(env), "utf-8");
}

function writeTradersJson(addresses) {
  const payload = {
    traders: addresses.map((address, index) => ({
      address,
      name: BOT_NAMES[index] || `Bot ${index + 1}`,
    })),
  };

  fs.writeFileSync(TRADERS_PATH, JSON.stringify(payload, null, 2), "utf-8");
}

function startHardhatNode() {
  return new Promise((resolve, reject) => {
    const child = spawn("yarn", ["hhnode"], {
      cwd: ROOT,
      shell: true,
    });

    let stdoutBuffer = "";
    let resolved = false;

    const accounts = [];
    const privateKeys = [];

    child.stdout.on("data", (chunk) => {
      const text = chunk.toString();
      process.stdout.write(text);
      stdoutBuffer += text;

      const accountRegex = /Account #\d+:\s+(0x[a-fA-F0-9]{40})/g;
      const pkRegex = /Private Key:\s+(0x[a-fA-F0-9]{64})/g;

      let match;

      while ((match = accountRegex.exec(stdoutBuffer)) !== null) {
        const address = match[1].toLowerCase();
        if (!accounts.includes(address)) accounts.push(address);
      }

      while ((match = pkRegex.exec(stdoutBuffer)) !== null) {
        const privateKey = match[1].toLowerCase();
        if (!privateKeys.includes(privateKey)) privateKeys.push(privateKey);
      }

      if (
        !resolved &&
        stdoutBuffer.includes("Started HTTP and WebSocket JSON-RPC server") &&
        accounts.length >= 9 &&
        privateKeys.length >= 9
      ) {
        resolved = true;
        resolve({ child, accounts, privateKeys });
      }
    });

    child.stderr.on("data", (chunk) => {
      process.stderr.write(chunk.toString());
    });

    child.on("error", reject);

    child.on("exit", (code) => {
      if (!resolved) {
        reject(new Error(`hardhat node exited early with code ${code}`));
      }
    });
  });
}

function runDeploy() {
  return new Promise((resolve, reject) => {
    const child = spawn("yarn", ["deploy:local"], {
      cwd: ROOT,
      shell: true,
    });

    let stdoutBuffer = "";
    const result = {};

    child.stdout.on("data", (chunk) => {
      const text = chunk.toString();
      process.stdout.write(text);
      stdoutBuffer += text;

      const patterns = {
  EXCHANGE_ADDRESS: /EXCHANGE:\s+(0x[a-fA-F0-9]{40})/,
  TKN1_ADDRESS: /TKN1:\s+(0x[a-fA-F0-9]{40})/,
  TKN2_ADDRESS: /TKN2:\s+(0x[a-fA-F0-9]{40})/,
  TKN3_ADDRESS: /TKN3:\s+(0x[a-fA-F0-9]{40})/,
  TKN4_ADDRESS: /TKN4:\s+(0x[a-fA-F0-9]{40})/,
  TKN5_ADDRESS: /TKN5:\s+(0x[a-fA-F0-9]{40})/,
};

      for (const [key, regex] of Object.entries(patterns)) {
        const match = stdoutBuffer.match(regex);
        if (match) result[key] = match[1].toLowerCase();
      }
    });

    child.stderr.on("data", (chunk) => {
      process.stderr.write(chunk.toString());
    });

    child.on("error", reject);

    child.on("exit", (code) => {
      if (code !== 0) {
        reject(new Error(`deploy failed with code ${code}`));
        return;
      }

      resolve(result);
    });
  });
}

async function main() {
  console.log("=================================");
  console.log("Starting local bootstrap...");
  console.log("=================================");

  const { child: hhnode, accounts, privateKeys } = await startHardhatNode();

  console.log("=================================");
  console.log("Hardhat node detected.");
  console.log("Accounts found:", accounts.length);
  console.log("Private keys found:", privateKeys.length);
  console.log("=================================");

  const deployed = await runDeploy();

  const botAddresses = accounts.slice(1, 6);
  const botPrivateKeys = privateKeys.slice(1, 6);

const envValues = {
  RPC_URL: "http://127.0.0.1:8545",

  // Valor inicial usado no ranking.
  // Cada bot começa com 1000 unidades de cada um dos 5 tokens.
  // Como a referência inicial é TKN1, o valor estimado inicial é:
  // 1000 TKN1 + 1000 TKN2 + 1000 TKN3 + 1000 TKN4 + 1000 TKN5 ~= 5000 TKN1
  INITIAL_REFERENCE_VALUE: "5000",

  REFERENCE_TOKEN_SYMBOL: "TKN1",
  TRADERS_FILE: "traders.json",
  PORT: "3001",
  ...deployed,
};

  for (let i = 0; i < BOT_KEYS.length; i++) {
    envValues[BOT_KEYS[i]] = botPrivateKeys[i];
  }

  writeEnv(envValues);
  writeTradersJson(botAddresses);

  console.log("=================================");
  console.log(".env updated from real hhnode + deploy output.");
  console.log("traders.json updated from real hhnode output.");
  console.log("");
  console.log("Admin account  : ", accounts[0]);
  console.log("Bot traders    : ", botAddresses);
  console.log("Contracts      : ", deployed);
  console.log("");
  console.log("Hardhat node is still running.");
  console.log("Leave this terminal open.");
  console.log("=================================");

  process.on("SIGINT", () => {
    console.log("\nStopping hardhat node...");
    hhnode.kill();
    process.exit(0);
  });
}

main().catch((error) => {
  console.error("Bootstrap error:");
  console.error(error);
  process.exit(1);
});