/**
 * Orchestrator — manages the full lifecycle of the simulation:
 *   Hardhat node → Contract deployment → Market setup → Bots → Competition
 */

import { spawn } from "child_process";
import { EventEmitter } from "events";
import { readFileSync, existsSync } from "fs";
import path from "path";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
export const ROOT = path.resolve(__dirname, "..");
const ENV_PATH = path.join(ROOT, ".env");
const IS_WIN = process.platform === "win32";

export const ORCH_STATE = Object.freeze({
  IDLE: "IDLE",
  STARTING_NODE: "STARTING_NODE",
  DEPLOYING: "DEPLOYING",
  SETTING_UP: "SETTING_UP",
  STARTING_BOTS: "STARTING_BOTS",
  RUNNING: "RUNNING",
  STOPPING: "STOPPING",
  STOPPED: "STOPPED",
  ERROR: "ERROR",
});

const BOT_CONFIGS = [
  { module: "bots.causes.noiseBot", name: "Bot de Ruído" },
  { module: "bots.causes.shockBot", name: "Bot de Choque" },
  { module: "bots.causes.trendBot", name: "Bot de Tendência" },
  { module: "bots.causes.meanReversionBot", name: "Bot de Reversão à Média" },
  { module: "bots.causes.bot28867", name: "Bot28867" },
];

function resolvePythonCommand() {
  if (process.env.PYTHON_CMD && process.env.PYTHON_CMD.trim()) {
    return process.env.PYTHON_CMD.trim();
  }

  const candidates = IS_WIN
    ? [
        path.join(ROOT, ".venv", "Scripts", "python.exe"),
        path.join(ROOT, "venv", "Scripts", "python.exe"),
        "py",
        "python",
      ]
    : [
        "/usr/bin/python3",
        "python3",
        "/usr/bin/python",
        "python",
        path.join(ROOT, ".venv", "bin", "python"),
        path.join(ROOT, "venv", "bin", "python"),
      ];

  for (const candidate of candidates) {
    const looksLikePath =
      candidate.includes("/") || candidate.includes("\\") || path.isAbsolute(candidate);

    if (!looksLikePath) return candidate;

    if (existsSync(candidate)) return candidate;
  }

  return IS_WIN ? "py" : "python3";
}

class Orchestrator extends EventEmitter {
  constructor() {
    super();
    this.setMaxListeners(100);
    this.state = ORCH_STATE.IDLE;
    this.logs = [];
    this.hhNode = null;
    this.bots = [];
    this.deployed = false;
  }

  log(message, level = "INFO") {
    const entry = { t: Date.now(), level, message: String(message).trim() };
    if (!entry.message) return;

    this.logs.push(entry);
    if (this.logs.length > 1000) this.logs = this.logs.slice(-800);
    this.emit("log", entry);
    console.log(`[Orch:${level}] ${entry.message}`);
  }

  setState(s) {
    this.state = s;
    this.emit("stateChange", s);
    this.log(`State → ${s}`);
  }

  getStatus() {
    return {
      state: this.state,
      nodeRunning: Boolean(this.hhNode && !this.hhNode.killed),
      bots: this.bots.map(({ name, module, alive, exitCode, pid }) => ({
        name,
        module,
        alive,
        exitCode: exitCode ?? null,
        pid: pid ?? null,
      })),
      deployed: this.deployed,
      recentLogs: this.logs.slice(-150),
    };
  }

  reloadEnv() {
    try {
      if (!existsSync(ENV_PATH)) return;

      const content = readFileSync(ENV_PATH, "utf-8");

      for (const line of content.split(/\r?\n/)) {
        const t = line.trim();
        if (!t || t.startsWith("#")) continue;

        const eq = t.indexOf("=");
        if (eq === -1) continue;

        const k = t.slice(0, eq).trim();
        const v = t.slice(eq + 1).trim();

        process.env[k] = v;
      }

      this.log("Reloaded .env — new contract addresses in process.env");
    } catch (e) {
      this.log(`Failed to reload .env: ${e.message}`, "WARN");
    }
  }

  async isNodeRunning() {
    const { ethers } = await import("ethers");

    try {
      const p = new ethers.JsonRpcProvider(
        process.env.RPC_URL || "http://127.0.0.1:8545"
      );
      await p.getBlockNumber();
      return true;
    } catch {
      return false;
    }
  }

  _spawn(cmd, args, extraEnv = {}) {
    return spawn(cmd, args, {
      cwd: ROOT,
      shell: IS_WIN,
      env: { ...process.env, ...extraEnv },
    });
  }

  _runHardhatScript(scriptRelPath, extraEnv = {}) {
    return new Promise((resolve, reject) => {
      this.log(`Running: npx hardhat run ${scriptRelPath} --network localhost`);

      const proc = this._spawn(
        "npx",
        ["hardhat", "run", scriptRelPath, "--network", "localhost"],
        extraEnv
      );

      let stdout = "";

      proc.stdout.on("data", (d) => {
        const text = d.toString();
        stdout += text;

        for (const line of text.split("\n")) {
          const l = line.trim();
          if (l) this.log(l, "SCRIPT");
        }
      });

      proc.stderr.on("data", (d) => {
        const text = d.toString();

        for (const line of text.split("\n")) {
          const l = line.trim();
          if (l) this.log(l, l.toLowerCase().includes("error") ? "ERROR" : "WARN");
        }
      });

      proc.on("close", (code) => {
        if (code === 0) resolve(stdout);
        else reject(new Error(`Script ${scriptRelPath} exited with code ${code}`));
      });

      proc.on("error", reject);
    });
  }

  async startNode() {
    if (await this.isNodeRunning()) {
      this.log("Hardhat node already responding on port 8545 — reusing");
      return;
    }

    this.setState(ORCH_STATE.STARTING_NODE);
    this.log("Starting Hardhat node (npx hardhat node)...");

    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.log("Hardhat node startup timeout — proceeding anyway", "WARN");
        resolve();
      }, 30_000);

      this.hhNode = this._spawn("npx", ["hardhat", "node"]);

      this.hhNode.stdout.on("data", (d) => {
        const text = d.toString();

        for (const line of text.split("\n")) {
          const l = line.trim();
          if (l) this.log(l, "NODE");
        }

        if (
          text.includes("Started HTTP and WebSocket JSON-RPC server") ||
          text.includes("Listening on")
        ) {
          clearTimeout(timer);
          this.log("Hardhat node ready on http://127.0.0.1:8545");
          resolve();
        }
      });

      this.hhNode.stderr.on("data", (d) => {
        const text = d.toString();

        for (const line of text.split("\n")) {
          const l = line.trim();
          if (l) this.log(l, "NODE");
        }
      });

      this.hhNode.on("error", (err) => {
        clearTimeout(timer);
        this.log(`Hardhat node spawn error: ${err.message}`, "ERROR");
        reject(err);
      });

      this.hhNode.on("close", (code) => {
        clearTimeout(timer);
        this.hhNode = null;
        this.log(`Hardhat node exited (code=${code})`, code === 0 ? "INFO" : "WARN");
      });
    });
  }

  async deployContracts() {
    this.setState(ORCH_STATE.DEPLOYING);
    this.log("Deploying smart contracts...");
    await this._runHardhatScript("scripts/deploy.js");
    this.reloadEnv();
    this.deployed = true;
    this.log("Contracts deployed — .env updated with new addresses");
  }

  async setupMarket() {
    this.setState(ORCH_STATE.SETTING_UP);
    this.log("Creating pools and registering traders...");
    await this._runHardhatScript("scripts/setup.js");
    this.log("Market setup complete");
  }

  async startBots() {
    const alive = this.bots.filter((b) => b.alive);

    if (alive.length > 0) {
      this.log(`${alive.length} bots already running — skipping relaunch`, "WARN");
      return;
    }

    this.setState(ORCH_STATE.STARTING_BOTS);
    this.log(`Launching ${BOT_CONFIGS.length} bots...`);
    this.bots = [];

    for (const cfg of BOT_CONFIGS) {
      this._launchBot(cfg);
      await new Promise((r) => setTimeout(r, 300));
    }

    this.log("All bots launched");
    this.setState(ORCH_STATE.RUNNING);
  }

  _launchBot(cfg, restartCount = 0) {
    const MAX_RESTARTS = 5;
    const RESTART_DELAY = 5_000;

    const pythonCmd = resolvePythonCommand();

    if (restartCount === 0) {
      this.log(`[${cfg.name}] using Python runtime: ${pythonCmd}`);
    }

    const proc = this._spawn(pythonCmd, ["-m", cfg.module]);

    const entry = {
      name: cfg.name,
      module: cfg.module,
      process: proc,
      alive: true,
      exitCode: null,
      pid: proc.pid ?? null,
    };

    proc.stdout.on("data", (d) => {
      const text = d.toString().trim();
      if (text) this.log(`[${cfg.name}] ${text}`, "BOT");
    });

    proc.stderr.on("data", (d) => {
      const text = d.toString().trim();
      if (text) this.log(`[${cfg.name}] ${text}`, "WARN");
    });

    proc.on("close", (code) => {
      entry.alive = false;
      entry.exitCode = code;

      this.emit("botExited", cfg.name, code);

      this.log(
        `[${cfg.name}] process exited (code=${code})`,
        code === 0 ? "INFO" : "WARN"
      );

      if (this.state !== ORCH_STATE.RUNNING) return;

      if (restartCount >= MAX_RESTARTS) {
        this.log(
          `[${cfg.name}] max restarts (${MAX_RESTARTS}) reached — will not restart`,
          "ERROR"
        );
        return;
      }

      const nextCount = restartCount + 1;

      this.log(
        `[${cfg.name}] restarting in ${RESTART_DELAY / 1000}s (attempt ${nextCount}/${MAX_RESTARTS})`,
        "WARN"
      );

      setTimeout(() => {
        if (this.state !== ORCH_STATE.RUNNING) return;

        const idx = this.bots.indexOf(entry);
        const newEntry = this._launchBot(cfg, nextCount);

        if (idx !== -1 && newEntry) this.bots[idx] = newEntry;

        this.emit("botExited");
      }, RESTART_DELAY);
    });

    proc.on("error", (err) => {
      entry.alive = false;
      entry.exitCode = -1;
      this.log(`[${cfg.name}] spawn error using "${pythonCmd}": ${err.message}`, "ERROR");
      this.emit("botExited", cfg.name, -1);
    });

    if (restartCount === 0) this.bots.push(entry);
    if (restartCount === 0) this.log(`Launched: ${cfg.name}`);

    return entry;
  }

  stopBots() {
    const running = this.bots.filter((b) => b.alive);

    if (!running.length) {
      this.log("No bots running");
      return;
    }

    this.log(`Sending SIGTERM to ${running.length} bots...`);

    for (const b of running) {
      try {
        b.process.kill("SIGTERM");
      } catch {}
    }

    setTimeout(() => {
      for (const b of this.bots) {
        if (b.alive) {
          try {
            b.process.kill("SIGKILL");
          } catch {}
        }
      }
    }, 2000);
  }

  async stop() {
    if (this.state === ORCH_STATE.IDLE || this.state === ORCH_STATE.STOPPED) return;

    this.setState(ORCH_STATE.STOPPING);
    this.stopBots();

    await new Promise((r) => setTimeout(r, 1500));

    this.setState(ORCH_STATE.STOPPED);
    this.log("Application stopped — click Start Application to resume");
  }

  async reset() {
    this.log("Resetting system...");
    this.stopBots();

    if (this.hhNode && !this.hhNode.killed) {
      try {
        this.hhNode.kill("SIGTERM");
      } catch {}
      this.hhNode = null;
    }

    await new Promise((r) => setTimeout(r, 500));

    this.bots = [];
    this.deployed = false;
    this.logs = [];

    this.setState(ORCH_STATE.IDLE);
    this.log("System reset complete — ready for a fresh start");
  }

  registerShutdownHook() {
    const cleanup = () => {
      for (const b of this.bots) {
        try {
          b.process.kill();
        } catch {}
      }

      if (this.hhNode) {
        try {
          this.hhNode.kill();
        } catch {}
      }
    };

    process.on("exit", cleanup);

    process.on("SIGINT", () => {
      cleanup();
      process.exit(0);
    });

    process.on("SIGTERM", () => {
      cleanup();
      process.exit(0);
    });
  }
}

export const orchestrator = new Orchestrator();
orchestrator.registerShutdownHook();
