#!/usr/bin/env python3
"""Controle local de ar-condicionado Midea (tipo 0xAC) via protocolo MSmart UDP.

Dependência: msmart-ng (pip install msmart-ng, reengenharia comunitária do
protocolo do app MSmartHome `com.midea.ai.overseas`).

O script fala DIRETO com o aparelho na rede local (UDP 6444, porta de descoberta
6445). Dispositivos protocolo V3 precisam de um par token/key obtido da nuvem
Midea UMA ÚNICA VEZ com a sua conta MSmartHome; depois disso, nenhuma nuvem é
usada. Tokens/key são salvos em devices.json ao lado deste script.

Credenciais da nuvem (só para o primeiro discover):
- flags --account/--password, ou
- variáveis de ambiente MIDEA_ACCOUNT / MIDEA_PASSWORD
- região via --region (padrão US; teste também --region EU se falhar).

Uso:
  python midea_ac.py discover [ip] [--account A --password P --region US]
  python midea_ac.py state <ip>
  python midea_ac.py on <ip> | off <ip>
  python midea_ac.py temp <ip> 23.5
  python midea_ac.py mode <ip> auto|cool|dry|heat|fan
  python midea_ac.py fan  <ip> auto|silent|low|medium|high|max
  python midea_ac.py swing <ip> off|vertical|horizontal|both
  python midea_ac.py display <ip> on|off
  python midea_ac.py eco <ip> on|off | turbo <ip> on|off
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

# Em exe (PyInstaller) __file__ aponta para a pasta temporária de extração —
# os dados do usuário devem ficar ao lado do .exe. No script normal, o .json
# fica ao lado do código.
if getattr(sys, "frozen", False):
    STORE = Path(sys.executable).with_name("devices.json")
else:
    STORE = Path(__file__).with_name("devices.json")

MODES = {"auto": "AUTO", "cool": "COOL", "dry": "DRY", "heat": "HEAT", "fan": "FAN_ONLY"}
FANS = {"auto": "AUTO", "silent": "SILENT", "low": "LOW", "medium": "MEDIUM", "high": "HIGH", "max": "MAX"}
SWINGS = {"off": "OFF", "vertical": "VERTICAL", "horizontal": "HORIZONTAL", "both": "BOTH"}


def _load() -> dict:
    if STORE.exists():
        try:
            return json.loads(STORE.read_text())
        except Exception:
            return {}
    return {}


def _save(data: dict) -> None:
    STORE.write_text(json.dumps(data, indent=2))


def _creds(args: argparse.Namespace) -> tuple[str | None, str | None]:
    return (args.account or os.environ.get("MIDEA_ACCOUNT"),
            args.password or os.environ.get("MIDEA_PASSWORD"))


async def _get_device(ip: str, region: str, account: str | None, password: str | None):
    from msmart.discover import Discover

    # auto_connect=False: não tenta conectar sem token primeiro
    device = await Discover.discover_single(ip, auto_connect=False)
    if not device:
        raise RuntimeError(f"Dispositivo não respondendo em {ip} (porta UDP 6444). AC na rede?")

    store = _load()
    entry = store.get(str(device.id)) or {}

    token, key = entry.get("token"), entry.get("key")
    authed = False

    if token and key:
        with contextlib.suppress(Exception):
            await device.authenticate(token, key)
            authed = True

    if not authed and account and password:
        authed = await _cloud_auth(device, region, account, password)

    if not authed:
        raise RuntimeError(
            f"Sem token/key para o dispositivo {device.id}.\n"
            "Execute: python midea_ac.py discover " + ip + " --account SEU_EMAIL --password SUA_SENHA\n"
            "(a conta MSmartHome precisa ser a dona do aparelho)"
        )

    store[str(device.id)] = {**entry, "token": device.token, "key": device.key,
                             "protocol": device.version, "type": hex(device.type),
                             "model": entry.get("model") or str(getattr(device, "sn", ""))}
    _save(store)
    return device


async def _cloud_auth(device, region: str, account: str, password: str,
                      diagnostics: list[str] | None = None, cloud=None) -> bool:
    from msmart.cloud import CloudError, SmartHomeCloud
    from msmart.lan import Security

    if cloud is None:
        cloud = SmartHomeCloud(region=region, account=account, password=password)
        try:
            await cloud.login()
        except Exception as e:
            if diagnostics is not None:
                diagnostics.append(f"Login na nuvem: {type(e).__name__}: {e}")
            raise RuntimeError(f"Falha login nuvem Midea ({region}): {e}")

    # tenta as duas ordens de bytes do ID para derivar o udpid
    for endian in ("little", "big"):
        udpid = Security.udpid(device.id.to_bytes(6, endian)).hex()
        try:
            token, key = await cloud.get_token(udpid, device.id)
        except CloudError as e:
            if diagnostics is not None:
                diagnostics.append(f"Obtenção de token ({endian}): {type(e).__name__}: {e}")
            print(f"  getToken({endian}) falhou: {e}")
            continue
        try:
            await device.authenticate(token, key)
            return True
        except Exception as e:
            if diagnostics is not None:
                diagnostics.append(f"Autenticação local ({endian}): {type(e).__name__}: {e}")
            continue
    if diagnostics is not None and not diagnostics:
        diagnostics.append("A nuvem não retornou token correspondente ao aparelho.")
    return False


def _dump(device) -> None:
    d = device.to_dict()
    print(json.dumps(_strip_bytes(d), ensure_ascii=False, indent=2, default=str))


def _strip_bytes(obj):
    if isinstance(obj, bytes):
        return obj.hex()
    if isinstance(obj, dict):
        return {k: _strip_bytes(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_strip_bytes(v) for v in obj]
    return obj


async def discover(ip: str, region: str, account: str | None, password: str | None) -> None:
    from msmart.discover import Discover

    if ip:
        device = await Discover.discover_single(ip, auto_connect=False, region=region)
        devices = [device] if device else []
    else:
        devices = await Discover.discover(region=region, auto_connect=False)

    if not devices:
        raise SystemExit("Nenhum aparelho encontrado na rede (broadcast 6445).")

    for device in devices:
        print(f"- id={device.id} ip={device.ip} porta={device.port} "
              f"tipo={hex(device.type)} protocolo={device.version} "
              f"suportado={'sim' if device.supported else 'NÃO'} modelo={getattr(device, 'sn', '')}")
    print()

    # autentica e persiste token/key de cada AC suportado
    for device in devices:
        if device.type not in (0xAC, 0xCC):
            print(f"id={device.id}: tipo {hex(device.type)} não é ar-condicionado; pulando.")
            continue
        if not account or not password:
            print(f"id={device.id}: forneça --account/--password para obter token/key. "
                  "Ou cole o token/key manualmente em devices.json.")
            continue
        if await _cloud_auth(device, region, account, password):
            store = _load()
            store[str(device.id)] = {"token": device.token, "key": device.key,
                                     "protocol": device.version, "ip": device.ip,
                                     "type": hex(device.type)}
            _save(store)
            print(f"id={device.id}: token/key salvos em {STORE.name} ✔")
        else:
            print(f"id={device.id}: não conseguiu autenticar na nuvem/local ✖")


async def _apply_and_print(device, message: str) -> None:
    await device.apply()
    await device.refresh()
    print(f"{message}:")
    print(f"  ligado={device.power_state} modulo={device.operational_mode.name} "
          f"alvo={device.target_temperature}°C vento={device.fan_speed.name} "
          f"swing={device.swing_mode.name} indoor={device.indoor_temperature}°C")
    if device.error_code:
        print(f"  ERRO APARELHO: {device.error_code}")


def _enum(cls, name):
    return cls[name.upper()]


async def async_main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)

    # opções comuns aceitas ANTES ou DEPOIS do comando (SUPPRESS evita o
    # subparser sobrescrever o que o parser principal já capturou)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--region", default=argparse.SUPPRESS)
    common.add_argument("--account", default=argparse.SUPPRESS)
    common.add_argument("--password", default=argparse.SUPPRESS)

    parser.add_argument("--region", default="US", help="região nuvem Midea (US, EU, ...)")
    parser.add_argument("--account")
    parser.add_argument("--password")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add(name, positional: bool = True):
        p = sub.add_parser(name, parents=[common])
        if positional:
            p.add_argument("ip")
        return p

    add("discover", positional=False).add_argument("ip", nargs="?", default=None, metavar="IP")
    add("state")
    add("on")
    add("off")
    p = add("temp"); p.add_argument("value", type=float)
    p = add("mode"); p.add_argument("value", choices=list(MODES))
    p = add("fan"); p.add_argument("value", choices=list(FANS))
    p = add("swing"); p.add_argument("value", choices=list(SWINGS))
    p = add("display"); p.add_argument("value", choices=("on", "off"))
    p = add("eco"); p.add_argument("value", choices=("on", "off"))
    p = add("turbo"); p.add_argument("value", choices=("on", "off"))

    args = parser.parse_args()

    if args.cmd == "discover":
        await discover(args.ip, args.region, args.account or os.environ.get("MIDEA_ACCOUNT"),
                       args.password or os.environ.get("MIDEA_PASSWORD"))
        return

    ip = args.ip
    device = await _get_device(ip, args.region, args.account, args.password)

    if args.cmd == "state":
        await device.refresh()
        print(f"ligado={device.power_state} modo={device.operational_mode.name} "
              f"alvo={device.target_temperature}C vento={device.fan_speed.name} "
              f"swing={device.swing_mode.name}")
        print(f"ambient={device.indoor_temperature}C outdoor={device.outdoor_temperature}C "
              f"umid={device.indoor_humidity}% eco={device.eco} turbo={device.turbo} "
              f"erro={device.error_code}")
        _dump(device)
        return

    if args.cmd == "on":
        device.power_state = True
    elif args.cmd == "off":
        device.power_state = False
    elif args.cmd == "temp":
        device.target_temperature = args.value
    elif args.cmd == "mode":
        device.operational_mode = _enum(device.OperationalMode, MODES[args.value])
        device.power_state = True
    elif args.cmd == "fan":
        device.fan_speed = _enum(device.FanSpeed, FANS[args.value])
    elif args.cmd == "swing":
        if device.swing_mode != device.SwingMode.OFF and args.value == "off":
            device.swing_mode = device.SwingMode.OFF
        elif args.value != "off":
            device.swing_mode = _enum(device.SwingMode, SWINGS[args.value])
    elif args.cmd == "display":
        if not device.supports_display_control:
            raise SystemExit("Aparelho não suporta controle do display.")
        state = await device.refresh() or (device.display_on, )
        # display: usa toggle apenas quando difere
        want = args.value == "on"
        if bool(device.display_on) != want:
            await device.toggle_display()
            return
        print("Display já está no estado desejado.")
        return
    elif args.cmd == "eco":
        device.eco = args.value == "on"
    elif args.cmd == "turbo":
        device.turbo = args.value == "on"

    await _apply_and_print(device, f"Comando '{args.cmd}' enviado")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        asyncio.run(async_main())
    except RuntimeError as e:
        # mensagens de uso para o usuário da CLI (antes era SystemExit para
        # sair bonito — RuntimeError foi adotado para nunca matar o painel)
        sys.exit(str(e))


if __name__ == "__main__":
    main()
