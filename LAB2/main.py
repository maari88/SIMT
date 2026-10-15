import paramiko
import json
import docker
import datetime
import requests
import time
import csv
import sys

SSH_HOST = "127.0.0.1"
SSH_PORT = 22
SSH_USER = "asa_null540"
SSH_PASSWORD = "****"
PROJECT_NAME = "lab-docker"


def run_command(ssh, cmd):
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=10)
    return stdout.read().decode('utf-8').strip()


def get_cli_inventory(ssh):
    print("\n[*] Збір даних через Docker CLI...")
    inventory = []
    cmd_ps = f"docker ps -a --no-trunc --filter label=com.docker.compose.project={PROJECT_NAME} --format '{{{{.ID}}}} {{{{.Image}}}} {{{{.State}}}}'"
    out_ps = run_command(ssh, cmd_ps)
    if not out_ps: return inventory

    for line in out_ps.split('\n'):
        parts = line.split()
        if len(parts) < 3: continue
        c_id, c_image, c_state = parts[0], parts[1], parts[2]

        cmd_inspect = f"docker inspect --format '{{{{index .Config.Labels \"com.docker.compose.service\"}}}} {{{{.State.StartedAt}}}}' {c_id}"
        out_inspect = run_command(ssh, cmd_inspect).split()
        c_service = out_inspect[0] if len(out_inspect) > 0 else "unknown"

        cmd_net = f"docker inspect --format '{{{{range $name, $network := .NetworkSettings.Networks}}}}{{{{println $name $network.IPAddress}}}}{{{{end}}}}' {c_id}"
        out_net = run_command(ssh, cmd_net)
        c_networks = {}
        for net_line in out_net.split('\n'):
            net_parts = net_line.split()
            if len(net_parts) == 2:
                c_networks[net_parts[0]] = net_parts[1]
            elif len(net_parts) == 1:
                c_networks[net_parts[0]] = ""

        c_ports = []
        out_port = run_command(ssh, f"docker port {c_id}")
        if out_port: c_ports = [p.strip() for p in out_port.split('\n') if p.strip()]

        inventory.append({
            "id": c_id, "service": c_service, "image": c_image,
            "state": c_state, "networks": c_networks, "ports": c_ports
        })
    return inventory


def get_api_inventory():
    print("\n[*] Збір даних через Docker SDK (API)...")
    inventory = []
    try:
        client = docker.DockerClient(base_url=f"ssh://{SSH_USER}@{SSH_HOST}")
    except Exception as e:
        print(f"[-] Помилка: {e}")
        return inventory

    containers = client.containers.list(all=True, filters={"label": f"com.docker.compose.project={PROJECT_NAME}"})
    for container in containers:
        c_id = container.id
        c_service = container.labels.get("com.docker.compose.service", "unknown")
        c_image = container.attrs["Config"]["Image"]
        c_state = container.attrs["State"]["Status"]

        c_networks = {}
        networks_data = container.attrs["NetworkSettings"].get("Networks", {})
        if networks_data:
            for net_name, net_info in networks_data.items():
                c_networks[net_name] = net_info.get("IPAddress", "")

        c_ports = []
        ports_data = container.attrs["NetworkSettings"].get("Ports", {})
        if ports_data:
            for port_key, port_bindings in ports_data.items():
                if port_bindings:
                    for binding in port_bindings:
                        host_ip = binding.get('HostIp', '0.0.0.0')
                        if host_ip == '::':
                            host_ip = '[::]'
                        c_ports.append(f"{port_key} -> {host_ip}:{binding.get('HostPort', '')}")

        inventory.append({
            "id": c_id, "service": c_service, "image": c_image,
            "state": c_state, "networks": c_networks, "ports": c_ports
        })
    return inventory


def build_snapshot(inventory_data, source_type):
    return {
        "host": SSH_HOST, "project": PROJECT_NAME,
        "timestamp": datetime.datetime.now().isoformat(),
        "source": source_type, "containers": inventory_data
    }


def verify_security(inventory_data):
    print("\n[*] Перевірка безпеки...")
    violations = []
    for c in inventory_data:
        if c["service"] != "proxy" and c["ports"]:
            violations.append(f"{c['service']} має відкриті порти: {c['ports']}")

    if not violations:
        print("   [+] ВІДПОВІДАЄ ВИМОГАМ.")
        return True
    else:
        print("   [-] ПОРУШЕННЯ:")
        for v in violations: print(f"       {v}")
        return False


def check_http_app():
    print("\n[*] HTTP-перевірка...")
    url = "http://127.0.0.1:8080"
    success = False
    for i in range(1, 3):
        try:
            start = time.time()
            response = requests.get(url, timeout=5)
            print(f"   [+] Запит {i}: Статус {response.status_code} ({time.time() - start:.3f} с)")
            if response.status_code == 200: success = True
        except Exception as e:
            print(f"   [-] Помилка: {e}")
        time.sleep(1)
    return success


def compare_inventories(cli_data, api_data):
    print("\n[*] Порівняння результатів CLI та API...")
    differences = []
    cli_dict = {c["id"]: c for c in cli_data}
    api_dict = {c["id"]: c for c in api_data}

    all_ids = set(cli_dict.keys()).union(set(api_dict.keys()))
    for c_id in all_ids:
        if c_id not in cli_dict:
            differences.append(f"Контейнер {c_id[:8]} відсутній в CLI")
            continue
        if c_id not in api_dict:
            differences.append(f"Контейнер {c_id[:8]} відсутній в API")
            continue

        if cli_dict[c_id]["state"] != api_dict[c_id]["state"]:
            differences.append(
                f"[{cli_dict[c_id]['service']}] Різний стан: {cli_dict[c_id]['state']} != {api_dict[c_id]['state']}")

        if set(cli_dict[c_id]["ports"]) != set(api_dict[c_id]["ports"]):
            differences.append(
                f"[{cli_dict[c_id]['service']}] Різні порти: CLI={cli_dict[c_id]['ports']}, API={api_dict[c_id]['ports']}")

    if not differences:
        print("   [+] Дані CLI та API повністю збігаються!")
    else:
        print("   [-] Знайдено відмінності:")
        for d in differences: print(f"       {d}")
    return differences


def save_results(snapshot, security_ok, http_ok, differences):
    print("\n[*] Збереження результатів...")

    with open("inventory.json", "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)
    print("   [+] Інвентаризацію збережено у 'inventory.json'")

    with open("report.csv", "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Check", "Status", "Details"])
        writer.writerow(["Security (Ports)", "Passed" if security_ok else "Failed",
                         "No extra ports" if security_ok else "Violations"])
        writer.writerow(["HTTP Check", "Passed" if http_ok else "Failed", "App responds"])
        writer.writerow(["CLI vs API", "Matches" if not differences else "Differs",
                         "None" if not differences else "; ".join(differences)])
    print("   [+] Звіт перевірок збережено у 'report.csv'")


# --- Моніторинг ---
def run_monitoring(interval):
    print(f"\n[***] Запуск періодичного моніторингу (інтервал {interval} с). Натисніть Ctrl+C для зупинки [***]")

    with open("monitoring.csv", "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Time", "HTTP_Status", "Proxy_State", "App_State", "DB_State"])

    try:
        client = docker.DockerClient(base_url=f"ssh://{SSH_USER}@{SSH_HOST}")
    except Exception as e:
        print(f"[-] Помилка підключення SDK: {e}")
        return

    url = "http://127.0.0.1:8080"

    try:
        while True:
            current_time = datetime.datetime.now().strftime("%H:%M:%S")

            # 1. Перевірка HTTP
            http_status = "Error"
            try:
                resp = requests.get(url, timeout=3)
                http_status = str(resp.status_code)
            except Exception:
                pass

            # 2. Збір станів контейнерів
            containers = client.containers.list(all=True,
                                                filters={"label": f"com.docker.compose.project={PROJECT_NAME}"})
            states = {"proxy": [], "app": [], "db": []}

            for c in containers:
                svc = c.labels.get("com.docker.compose.service", "unknown")
                c.reload()  # ОНОВЛЮЄМО дані напряму з демона
                status = c.attrs["State"]["Status"]
                if svc in states:
                    states[svc].append(status)

            proxy_st = ", ".join(states["proxy"]) if states["proxy"] else "none"
            app_st = ", ".join(states["app"]) if states["app"] else "none"
            db_st = ", ".join(states["db"]) if states["db"] else "none"

            # Вивід на екран
            print(f"[{current_time}] HTTP: {http_status:<5} | Proxy: {proxy_st:<7} | App: {app_st:<15} | DB: {db_st}")

            # 3. Запис у CSV
            with open("monitoring.csv", "a", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([current_time, http_status, proxy_st, app_st, db_st])

            time.sleep(interval)

    except KeyboardInterrupt:
        print("\n[*] Моніторинг зупинено користувачем.")


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--monitor":
        interval = int(sys.argv[2])
        run_monitoring(interval)
        return

    # Якщо без параметрів - виконуємо основну лабу
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(hostname=SSH_HOST, port=SSH_PORT, username=SSH_USER, password=SSH_PASSWORD)
        cli_data = get_cli_inventory(ssh)
    finally:
        ssh.close()

    api_data = get_api_inventory()

    if not cli_data or not api_data:
        print("[-] Немає даних для аналізу.")
        return

    snapshot = build_snapshot(api_data, "Docker Engine API")
    sec_ok = verify_security(api_data)
    http_ok = check_http_app()
    diffs = compare_inventories(cli_data, api_data)
    save_results(snapshot, sec_ok, http_ok, diffs)

    print("\nЗАВДАННЯ ВИКОНАНО УСПІШНО!\n")


if __name__ == "__main__":
    main()