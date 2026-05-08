import sys
import time
import signal
import subprocess

BOT_MODULES = [
    "bots.causes.noiseBot",
    "bots.causes.shockBot",
    "bots.causes.trendBot",
    "bots.causes.meanReversionBot",
    "bots.causes.bot28867",
]

processes = []


def start_bots():
    print("A iniciar bots...\n")

    for module_name in BOT_MODULES:
        print(f"Iniciando: {module_name}")

        process = subprocess.Popen(
            [sys.executable, "-m", module_name],
            stdout=None,
            stderr=None
        )

        processes.append(process)

    print("\nBots iniciados.")
    print("CTRL+C para parar.\n")


def stop_bots():
    print("\nA parar bots...")

    for process in processes:
        if process.poll() is None:
            process.terminate()

    time.sleep(1)

    for process in processes:
        if process.poll() is None:
            process.kill()

    print("Bots encerrados.")


def handle_exit(signum, frame):
    stop_bots()
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    start_bots()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop_bots()