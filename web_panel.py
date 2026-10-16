#!/usr/bin/env python3
"""Painel de ar-condicionado Midea — UI web (design do usuário) em janela
nativa frameless via pywebview/WebView2 + ícone na system tray (pystray).
Os comandos vão DIRETO ao aparelho via UDP 6444 (msmart-ng), sem nuvem.

Uso:  py web_panel.py [--ip 192.168.1.20] [--region US]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import ipaddress
import socket
import sys
import threading
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
if sys.stdout is not None:
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent
if getattr(sys, "frozen", False):
    # exe (PyInstaller): dados do usuário ao lado do .exe,
    # recursos empacotados (ui/) vêm da pasta de extração (_MEIPASS).
    HERE = Path(sys.executable).parent
    RES = Path(getattr(sys, "_MEIPASS", HERE))
else:
    RES = HERE
logger = logging.getLogger("web_panel")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    _file_log = logging.FileHandler(HERE / "web_panel.log", encoding="utf-8")
    _file_log.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_file_log)
sys.path.insert(0, str(HERE))


def _safe_auth_diagnostic(messages: list[str], account: str, password: str) -> str:
    """Remove login secrets before returning auth details to UI or log."""
    safe = []
    for message in messages:
        value = str(message).replace(account, "[conta]") if account else str(message)
        value = value.replace(password, "[senha]") if password else value
        value = " ".join(value.split())
        if value and value not in safe:
            safe.append(value[:300])
    return " | ".join(safe)[:700] or "Sem detalhes fornecidos pela autenticação."


async def _scan_local_subnets(region: str) -> list:
    """Fallback Midea discovery using unicast probes on each local IPv4 subnet."""
    try:
        import psutil
        from msmart.discover import DISCOVERY_MSG, Discover, DiscoverError
    except ImportError:
        logger.warning("Subnet discovery unavailable: psutil is not installed")
        return []

    # Discover stores these values at class level; keep fallback probes
    # consistent with the regular msmart broadcast scan above.
    Discover._region = region
    Discover._account = None
    Discover._password = None
    Discover._auto_connect = False
    Discover._cloud = None
    networks = {}
    stats = psutil.net_if_stats()
    for name, addresses in psutil.net_if_addrs().items():
        interface = stats.get(name)
        if not interface or not interface.isup:
            continue
        for address in addresses:
            if address.family != socket.AF_INET or not address.netmask:
                continue
            try:
                network = ipaddress.ip_network(
                    f"{address.address}/{address.netmask}", strict=False)
            except ValueError:
                continue
            if address.address.startswith("127.") or network.num_addresses <= 2:
                continue
            # Avoid emitting thousands of packets on unusually large LANs.
            if network.num_addresses > 1024:
                logger.info("Skipping oversized subnet scan %s on %s", network, name)
                continue
            networks.setdefault(str(network), address.address)

    if not networks:
        return []

    loop = asyncio.get_running_loop()
    devices = []
    for network_text, source_ip in networks.items():
        network = ipaddress.ip_network(network_text)
        discovered_ips = set()
        tasks = set()

        class ProbeProtocol(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                ip = addr[0]
                if ip in discovered_ips:
                    return
                try:
                    version = Discover._get_device_version(data)
                except DiscoverError:
                    return
                discovered_ips.add(ip)
                task = asyncio.create_task(Discover._get_device(ip, version, data))
                tasks.add(task)

        try:
            transport, _ = await loop.create_datagram_endpoint(
                ProbeProtocol, local_addr=(source_ip, 0))
        except OSError as exc:
            logger.warning("Cannot scan subnet %s via %s: %s",
                           network, source_ip, exc)
            continue

        try:
            sent = 0
            for host in network.hosts():
                target = str(host)
                if target == source_ip:
                    continue
                for port in (6445, 20086):
                    try:
                        transport.sendto(DISCOVERY_MSG, (target, port))
                        sent += 1
                    except OSError:
                        pass
                if sent and sent % 64 == 0:
                    await asyncio.sleep(0)
            logger.info("Sent %d Midea unicast probes on %s", sent, network)
            await asyncio.sleep(1.5)
        finally:
            transport.close()

        if tasks:
            replies = await asyncio.gather(*tasks, return_exceptions=True)
            devices.extend(d for d in replies if d and not isinstance(d, Exception))

    return devices

try:
    import webview  # pywebview
except Exception:
    logger.exception("Failed importing pywebview")
    raise


def snap(dev) -> dict:
    modes = getattr(dev, "supported_operation_modes", None) or []
    return {
        "ok": True,
        "power": bool(dev.power_state),
        "temp": float(dev.target_temperature),
        "mode": getattr(getattr(dev, "operational_mode", None), "name", "COOL"),
        "fan": getattr(getattr(dev, "fan_speed", None), "name", "AUTO"),
        "swing": getattr(getattr(dev, "swing_mode", None), "name", "OFF"),
        "eco": bool(getattr(dev, "eco", False)),
        "display_on": bool(getattr(dev, "display_on", True)),
        "ambient": dev.indoor_temperature,
        "modes": [m.name for m in modes
                  if m.name not in ("SMART_DRY", "HEAT")],  # esse AC não tem
    }


def _tray_mode_label(mode: str) -> str:
    return {"COOL": "Resfriando", "DRY": "Secando", "HEAT": "Aquecendo",
            "FAN_ONLY": "Ventilando", "SMART_DRY": "Secando",
            "AUTO": "Auto"}.get(mode, mode)


def make_state_icon(state: dict) -> "Image.Image":  # noqa: F821
    """Ícone da bandeja: linhas de vento + selo com a temperatura atual.
    Estado desconhecido → 'AC'; desligado → 'off' esmaecido."""
    from PIL import Image, ImageDraw, ImageFont

    on = bool(state.get("power"))
    temp = state.get("temp")
    temp = temp if isinstance(temp, (int, float)) and on else None

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    alpha = 255 if on else 90

    def row(y, x0, x1, r, direction, shade):
        color = shade[:3] + (alpha,)
        d.line((x0, y, x1, y), fill=color, width=4)
        if direction == "up":                     # gancho p/ cima no fim
            bbox = [x1 - r, y - 2 * r, x1 + r, y]
        else:                                     # gancho p/ baixo no começo
            bbox = [x0 - r, y, x0 + r, y + 2 * r]
        d.arc(bbox, start=270, end=450, fill=color, width=4)

    row(14, 14, 34, 6, "up",   (154, 162, 172))
    row(25,  6, 40, 7, "up",   (110, 118, 128))
    row(25, 40, 50, 5, "up",   (154, 162, 172))
    row(34, 14, 30, 6, "down", (74, 82, 96))

    unknown = "power" not in state
    if temp is not None:
        txt = f"{int(round(temp))}°"
        d.rounded_rectangle([7, 39, 57, 62], radius=11, fill=(13, 28, 52, 240))
        try:
            f = ImageFont.truetype("segoeuib.ttf", 20)
        except OSError:                               # noqa: PERF203
            f = ImageFont.load_default()
    elif unknown:
        txt, bg = "AC", (13, 28, 52, 170)
        d.rounded_rectangle([7, 39, 57, 62], radius=11, fill=bg)
        try:
            f = ImageFont.truetype("segoeuib.ttf", 15)
        except OSError:                               # noqa: PERF203
            f = ImageFont.load_default()
    else:
        txt, bg = "off", (13, 28, 52, 170)
        d.rounded_rectangle([7, 39, 57, 62], radius=11, fill=bg)
        try:
            f = ImageFont.truetype("segoeuib.ttf", 15)
        except OSError:                               # noqa: PERF203
            f = ImageFont.load_default()
    d.text((32, 50), txt, font=f, fill=(200, 228, 255, alpha), anchor="mm")
    return img


class App:
    def __init__(self, ip: str, region: str) -> None:
        self.ip, self.region = ip, region
        self.win = None
        self._device = None
        self._found_devices = {}
        self._cloud_session = None
        self._cloud_session_region = None
        self._last_fit_w = 372 + 8
        self._last_fit_h = 600
        # serializa comandos: o ar aceita 1 cliente UDP; cliques rápidos
        # entram em fila, sem disputar socket nem embaralhar applies
        self._dev_lock = asyncio.Lock()
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self._run_loop, daemon=True).start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.set_debug(False)

        async def _heartbeat():
            n = 0
            while True:
                await asyncio.sleep(10)
                n += 1
                logger.info("app loop vivo (t%d)", n)

        try:
            self.loop.create_task(_heartbeat())
        except Exception:                    # noqa: BLE001
            pass
        try:
            self.loop.run_forever()
        except BaseException as e:           # noqa: BLE001
            logger.exception("App loop CRASHEOU: %r", e)
        logger.warning(
            "App loop EXITED: closed=%s", self.loop.is_closed())

    def _ensure_loop(self) -> None:
        """Se o loop morreu por qualquer motivo, ressuscita numa thread nova
        (o painel continua vivo para o usuário, sem depender de reinício)."""
        if self.loop.is_running():
            return
        try:
            if not self.loop.is_closed():
                self.loop.call_soon_threadsafe(self.loop.stop)
                self.loop.close()
        except Exception:                    # noqa: BLE001
            pass
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self._run_loop, daemon=True).start()
        logger.warning("App loop recriado e reiniciado")

    def submit(self, coro) -> None:
        self._ensure_loop()
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        fut.add_done_callback(self._log_task_exc)

    @staticmethod
    def _log_task_exc(fut) -> None:
        if fut.cancelled():
            return
        exc = fut.exception()
        if exc and not isinstance(exc, asyncio.CancelledError):
            logger.error("Submitted task failed: %r", exc)

    def do(self, coro, timeout: int = 20):
        try:
            self._ensure_loop()
            return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)
        except Exception as e:                    # noqa: BLE001
            return None, str(e)

    async def _dev(self):
        if self._device is None:
            try:
                import midea_ac
                self._device = await midea_ac._get_device(self.ip, self.region, None, None)
            except BaseException as e:      # noqa: BLE001 — SystemExit/RuntimError/include
                if isinstance(e, asyncio.CancelledError):
                    raise
                logger.error("Dispositivo indisponível: %s", e)
                self._device = None
                raise
        return self._device

    # ---- tarefas ----
    def _state_changed(self, st) -> None:
        """Notifica o ícone da bandeja (definido em main())."""
        cb = getattr(self, "on_state_change", None)
        if not cb or not st:
            return
        try:
            cb(st)
        except Exception:                         # noqa: BLE001
            logger.exception("tray icon update failed")

    async def task_refresh(self):
        try:
            dev = await self._dev()
            await dev.refresh()
            st = snap(dev)
            self._state_changed(st)
            return st, None
        except Exception as e:                    # noqa: BLE001
            return None, str(e)

    async def task_login(self, account: str, password: str, region: str):
        """Validate the MSmartHome login and keep its session in memory."""
        if not account or not password:
            return {"ok": False, "stage": "login", "error": "Informe a conta e a senha MSmartHome."}
        if region not in {"US", "DE", "KR"}:
            return {"ok": False, "stage": "login", "error": "Selecione uma região válida para a conta MSmartHome."}

        logger.info("login step: pre-import")
        from msmart.cloud import SmartHomeCloud
        logger.info("login step: import ok")

        self._cloud_session = None
        self._cloud_session_region = None
        self._found_devices = {}
        cloud = SmartHomeCloud(region=region, account=account, password=password)
        logger.info("Starting MSmartHome login (region %s)", region)
        try:
            login_task = asyncio.create_task(cloud.login())
            done, _ = await asyncio.wait({login_task}, timeout=15)
            if not done:
                login_task.cancel()
                login_task.add_done_callback(
                    lambda task: None if task.cancelled() else task.exception())
                logger.warning("MSmartHome login timed out after 15 seconds (region %s)", region)
                return {"ok": False, "stage": "login",
                        "error": "O serviço MSmartHome não respondeu em 15 segundos. Confira a conexão e a região da conta."}
            await login_task
        except asyncio.TimeoutError:
            logger.warning("MSmartHome login timed out after 15 seconds (region %s)", region)
            return {"ok": False, "stage": "login",
                    "error": "O serviço MSmartHome não respondeu em 15 segundos. Confira a conexão e a região da conta."}
        except Exception as exc:  # noqa: BLE001
            detail = _safe_auth_diagnostic(
                [f"{type(exc).__name__}: {exc}"], account, password)
            logger.warning("MSmartHome login failed (%s): %s", region, detail)
            return {"ok": False, "stage": "login",
                    "error": f"Não foi possível entrar na conta MSmartHome. {detail}"}

        self._cloud_session = cloud
        self._cloud_session_region = region
        logger.info("MSmartHome login succeeded for region %s", region)
        return {"ok": True, "stage": "login"}

    async def task_discover_devices(self, account: str, password: str, region: str):
        """Find ACs after login and exchange cloud access for local keys."""
        if self._cloud_session is None or self._cloud_session_region != region:
            return {"ok": False, "stage": "discovery",
                    "error": "Faça login novamente antes de buscar aparelhos."}

        import contextlib
        import io
        import midea_ac
        from msmart.discover import Discover

        try:
            devices = await Discover.discover(region=region, auto_connect=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("LAN discovery failed (%s): %s", type(exc).__name__, exc)
            return {"ok": False, "stage": "discovery", "error": "Não consegui procurar na rede. Confira o Wi-Fi e tente de novo."}

        if not any(d.type in (0xAC, 0xCC) for d in devices):
            try:
                fallback_devices = await _scan_local_subnets(region)
                by_id = {str(d.id): d for d in devices}
                by_id.update({str(d.id): d for d in fallback_devices})
                devices = list(by_id.values())
            except Exception as exc:  # noqa: BLE001
                logger.exception("Subnet fallback discovery failed: %s", exc)

        air_conditioners = [d for d in devices if d.type in (0xAC, 0xCC)]
        if not air_conditioners:
            self._found_devices = {}
            return {"ok": True, "stage": "discovery", "devices": []}

        store = midea_ac._load()
        results = []
        found = {}
        for device in air_conditioners:
            device_id = str(device.id)
            entry = store.get(device_id) or {}
            authenticated = False
            auth_diagnostics = []
            if entry.get("token") and entry.get("key"):
                try:
                    await device.authenticate(entry["token"], entry["key"])
                    authenticated = True
                except Exception as exc:  # noqa: BLE001
                    auth_diagnostics.append(
                        f"Chave local rejeitada: {type(exc).__name__}: {exc}")

            if not authenticated:
                try:
                    # Captura prints da CLI; diagnósticos estruturados ficam
                    # disponíveis para a interface sem expor as credenciais.
                    with contextlib.redirect_stdout(io.StringIO()):
                        authenticated = await midea_ac._cloud_auth(
                            device, region, account, password, auth_diagnostics,
                            cloud=self._cloud_session)
                except (Exception, SystemExit) as exc:
                    authenticated = False
                    if not auth_diagnostics:
                        auth_diagnostics.append(
                            f"Autenticação interrompida: {type(exc).__name__}: {exc}")

            auth_reason = _safe_auth_diagnostic(auth_diagnostics, account, password)
            if not authenticated:
                logger.warning("Device auth failed id=%s region=%s: %s",
                               device_id, region,
                               _safe_auth_diagnostic(auth_diagnostics, account, password))

            model = str(getattr(device, "sn", "") or "Ar-condicionado Midea")
            item = {
                "id": device_id,
                "ip": str(device.ip),
                "model": model,
                "authenticated": bool(authenticated),
            }
            if not authenticated:
                item["reason"] = auth_reason
            results.append(item)
            if authenticated:
                store[device_id] = {
                    **entry,
                    "token": device.token,
                    "key": device.key,
                    "protocol": device.version,
                    "ip": device.ip,
                    "type": hex(device.type),
                    "model": model,
                }
                found[device_id] = {**item, "region": region}

        if found:
            midea_ac._save(store)
        self._found_devices = found
        return {"ok": True, "stage": "discovery", "devices": results}

    async def task_set(self, mutate, attrs=None):
        """Aplica mutate no ar e CONFIRMA lendo de volta. O ar às vezes
        ignora o primeiro frame (UDP local instável), então reenviamos o
        mesmo alvo até o estado aplicado bater com o pedido."""
        try:
            async with self._dev_lock:
                dev = await self._dev()
                expected = {}
                for attempt in range(4):
                    mutate(dev)
                    if attempt == 0:
                        for a in (attrs or []):
                            v = getattr(dev, a, None)
                            expected[a] = getattr(v, "name", v)
                    await dev.apply()
                    await asyncio.sleep(0.8)
                    await dev.refresh()
                    if not expected or all(
                        getattr(getattr(dev, a, None), "name", getattr(dev, a, None)) == v
                        for a, v in expected.items()
                    ):
                        break
                    logger.info("confirm: %s diverge (tentativa %d) — reenviando",
                                attrs, attempt + 1)
            st = snap(dev)
            self._state_changed(st)
            return st, None
        except Exception as e:                    # noqa: BLE001
            return None, str(e)

    async def task_cycle(self, attr, enum_name, order):
        target = {}

        def mutate(d):
            en = getattr(d, enum_name)
            if target:
                # reaplicação (retry): reenvia EXATAMENTE o alvo escolhido
                setattr(d, attr, en[target["name"]])
                return
            names = order
            if names is None:
                # usa a lista de modos que o aparelho declara suportar
                # (capability-driven, na ordem em que o Midea a fornece)
                sup = (getattr(d, "supported_operation_modes", None)
                       or getattr(d, "supported_modes", None) or [])
                names = [m.name for m in sup if m.name != "SMART_DRY"]
                if not names:
                    names = ["COOL", "HEAT", "FAN_ONLY", "DRY"]
            cur = getattr(d, attr).name
            nxt = names[(names.index(cur) + 1) % len(names)] if cur in names else names[0]
            target["name"] = nxt
            setattr(d, attr, en[nxt])
        return await self.task_set(mutate, attrs=[attr])

    # ---- janela ----
    def fit(self, w: int = 0, h: int = 0) -> None:
        """Encaixa o painel na área de trabalho e habilita o vidro nativo.
        RODA NA THREAD DE DISPATCH DO js_api (via Api.fit). Nunca submeta
        este método ao loop de eventos: evaluate_js/resize esperam a thread
        da UI e travam o loop (o login agendo não sai do lugar)."""
        w = int(w) if w else self._last_fit_w
        h = int(h) if h else self._last_fit_h
        self._last_fit_w, self._last_fit_h = w, h
        try:
            self.win.resize(w, h)
            x, y, aw, ah = _work_area()
            self.win.move(x + aw - w - 12, y + ah - h - 12)
            # NÃO toque em win.native aqui: esta thread é a de dispatch do
            # js_api — propriedades do Form/WebView2 só existem na UI thread
            # (na errada, atributos estopam COM e matam o dispatcher).
            # TOOLWINDOW/cantos/tema ficam no evento shown (main: on_native_ready).
            # modo app: card passa a preencher exatamente a janela
            self.win.evaluate_js("document.documentElement.classList.add('appmode')")
            self.win.evaluate_js(
                "document.querySelector('.head') && "
                "document.querySelector('.head').classList.add('pywebview-drag-region')")
        except Exception as e:                    # noqa: BLE001
            logger.exception("Window fit failed: %s", e)

    def show_panel(self) -> None:
        try:
            webview.windows[0].show()
        except Exception:
            pass

    def hide_panel(self) -> None:
        try:
            webview.windows[0].hide()
        except Exception:
            pass


class Api:
    """js_api: métodos chamados do JavaScript (window.pywebview.api.*)."""

    def __init__(self, app: App) -> None:
        self.app = app

    _EXPOSED_JS = (
        "login", "discover_devices", "select_device", "refresh",
        "set_temp", "set_power", "set_eco", "set_display",
        "set_mode", "set_fan", "set_dir",
        "cycle_fan", "cycle_dir", "cycle_mode", "fit", "hide_panel",
        "connection_info",
    )

    def __dir__(self):
        # pywebview caminha dir() deste objeto para expor funções ao JS.
        # Sem isto, ele recorre pelos atributos públicos (app.win.native…)
        # e explode em COM/pythonnet, matando a ponte antes de qualquer
        # chamada — por isso o login “não retornava”.
        return list(self._EXPOSED_JS)

    def login(self, account: str, password: str, region: str = "US") -> dict:
        result = self.app.do(
            self.app.task_login(account, password, region), timeout=25)
        # task_login devolve dict; do() pode devolver (None, erro) em falha
        if isinstance(result, tuple):
            result = result[0]
        if result is None or not isinstance(result, dict):
            import threading as _th
            logger.warning(
                "login timeout diagnostico: loop_running=%s threads=%s",
                self.app.loop.is_running(),
                [t.name for t in _th.enumerate()])
            return {"ok": False, "stage": "login",
                    "error": "O login não foi concluído. Verifique a conexão e tente novamente."}
        return result

    def discover_devices(self, account: str, password: str, region: str = "US") -> dict:
        result = self.app.do(
            self.app.task_discover_devices(account, password, region), timeout=120)
        if isinstance(result, tuple):
            result = result[0]
        if result is None or not isinstance(result, dict):
            return {"ok": False, "stage": "discovery",
                    "error": "A busca demorou demais ou falhou. Confira a rede e tente novamente."}
        return result

    def select_device(self, device_id: str) -> dict:
        device = self.app._found_devices.get(str(device_id))
        if not device:
            return {"ok": False, "error": "Esse aparelho não está autenticado. Faça a busca novamente."}

        self.app.ip = device["ip"]
        self.app.region = device["region"]
        self.app._device = None
        try:
            config_path = HERE / "tray_config.json"
            config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
            config.update(ip=self.app.ip, region=self.app.region)
            config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": "Não consegui salvar o aparelho selecionado."}

        state, error = self.app.do(self.app.task_refresh())
        if not state:
            return {"ok": False, "error": "Aparelho salvo, mas não consegui ler o estado agora."}
        return {"ok": True, "device": device, "state": state}

    def refresh(self) -> dict:
        st, err = self.app.do(self.app.task_refresh())
        return st or {"ok": False, "error": err}

    def connection_info(self) -> dict:
        """Estado da conexão atual (para a tela 'Conectar / buscar aparelho')."""
        app = self.app
        store = {}
        try:
            import midea_ac
            store = midea_ac._load()
        except Exception:                  # noqa: BLE001
            pass
        entry = store.get(str(getattr(app._device, "id", ""))) or {}
        dev = app._device
        if dev is not None:
            return {"ok": True, "connected": True,
                    "ip": getattr(dev, "ip", None) or app.ip,
                    "id": str(dev.id),
                    "model": entry.get("model") or getattr(dev, "sn", "") or "Ar-condicionado Midea"}
        return {"ok": True, "connected": False, "ip": app.ip,
                "model": entry.get("model") or None, "region": app.region}

    def set_temp(self, v: float) -> dict:
        st, err = self.app.do(self.app.task_set(
            lambda d: setattr(d, "target_temperature", float(v)),
            attrs=["target_temperature"]))
        return st or {"ok": False, "error": err}

    def set_power(self, on: bool) -> dict:
        st, err = self.app.do(self.app.task_set(
            lambda d: setattr(d, "power_state", bool(on)),
            attrs=["power_state"]))
        return st or {"ok": False, "error": err}

    def set_eco(self, on: bool) -> dict:
        st, err = self.app.do(self.app.task_set(
            lambda d: setattr(d, "eco", bool(on)),
            attrs=["eco"]))
        return st or {"ok": False, "error": err}

    def set_display(self, on: bool) -> dict:
        def mutate(d: "AirConditioner") -> None:  # noqa: F821
            if bool(d.display_on) != bool(on):
                try:
                    d.toggle_display()
                except Exception:                  # noqa: BLE001
                    pass
        st, err = self.app.do(self.app.task_set(mutate, attrs=["display_on"]))
        return st or {"ok": False, "error": err}

    def _set_named(self, attr, value: str, enum_name: str) -> dict:
        """Define um campo por NOME do enum (chamado com o alvo calculado
        no lado JS — a UI é óptima, os comandos chegam com o valor final)."""
        def mutate(d):
            setattr(d, attr, getattr(getattr(d, enum_name), value))
        st, err = self.app.do(self.app.task_set(mutate, attrs=[attr]))
        return st or {"ok": False, "error": err}

    def set_mode(self, name: str) -> dict:
        return self._set_named("operational_mode", name, "OperationalMode")

    def set_fan(self, name: str) -> dict:
        return self._set_named("fan_speed", name, "FanSpeed")

    def set_dir(self, name: str) -> dict:
        return self._set_named("swing_mode", name, "SwingMode")

    def cycle_fan(self) -> dict:
        return self._cycle("fan_speed", "FanSpeed", ["LOW", "MEDIUM", "HIGH"])

    def cycle_dir(self) -> dict:
        # só duas posições no painel: Fixa (OFF) ↔ Oscilar (VERTICAL)
        return self._cycle("swing_mode", "SwingMode", ["VERTICAL", "OFF"])

    def cycle_mode(self) -> dict:
        # order=None → o próprio aparelho diz os modos suportados e a ordem
        return self._cycle("operational_mode", "OperationalMode", None)

    def _cycle(self, attr, enum_name, order) -> dict:
        st, err = self.app.do(self.app.task_cycle(attr, enum_name, order))
        return st or {"ok": False, "error": err}

    def fit(self, w: int = 0, h: int = 0):
        """Encaixa da janela: roda direto aqui (thread de dispatch) — sem
        intermediar o loop de eventos, que deve ficar livre para login/AC."""
        self.app.win = self.app.win or (webview.windows[0] if webview.windows else None)
        self.app.fit(w, h)
        return "ok"

    def hide_panel(self) -> None:
        self.app.submit(self.app.hide_panel())


def _work_area() -> tuple[int, int, int, int]:
    """(x, y, largura, altura) da área de trabalho (exclui taskbar)."""
    import ctypes

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    rect = RECT()
    ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


def _bottom_right_x(w: int) -> int:
    x, _, aw, _ = _work_area()
    return x + aw - w - 12  # canto direito


def _bottom_left_y(h: int) -> int:
    _, y, _, ah = _work_area()
    return y + ah - h - 12  # grudado no rodapé da área útil


def _set_native_corners(hwnd) -> None:
    """Configura a janela do tray, os cantos e o tema escuro do Windows."""
    import ctypes
    try:
        hwnd = int(getattr(hwnd, "ToInt64", lambda: hwnd)())
    except Exception:  # noqa: BLE001
        hwnd = 0
    if not hwnd:
        return

    # TOOLWINDOW remove o botão da barra de tarefas (e a entrada no Alt+Tab);
    # o painel continua acessível pelo ícone da bandeja.
    user32 = ctypes.windll.user32
    get_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
    set_long = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
    get_long.argtypes = [ctypes.c_void_p, ctypes.c_int]
    get_long.restype = ctypes.c_ssize_t
    set_long.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
    set_long.restype = ctypes.c_ssize_t
    ex_style = get_long(hwnd, -20)  # GWL_EXSTYLE
    ex_style = (ex_style & ~0x00040000) | 0x00000080  # sem APPWINDOW, com TOOLWINDOW
    set_long(hwnd, -20, ex_style)
    user32.SetWindowPos(
        ctypes.c_void_p(hwnd), None, 0, 0, 0, 0,
        0x0001 | 0x0002 | 0x0004 | 0x0020 | 0x0010)

    dwm = ctypes.windll.dwmapi
    dwm.DwmSetWindowAttribute.restype = ctypes.c_long
    dwm.DwmSetWindowAttribute.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
    # Usa o arredondamento nativo do Windows 11.
    corner = ctypes.c_int(2)  # DWMWCP_ROUND
    dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corner), 4)
    # DWMWA_BORDER_COLOR + DWMWA_COLOR_NONE: evita uma segunda borda do Windows.
    no_system_border = ctypes.c_uint(0xFFFFFFFE)
    dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(no_system_border), 4)
    dark = ctypes.c_int(1)  # DWMWA_USE_IMMERSIVE_DARK_MODE
    dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), 4)


def load_settings(a) -> tuple[str, str]:
    cfg_p = HERE / "tray_config.json"
    cfg = json.loads(cfg_p.read_text()) if cfg_p.exists() else {}
    dv_p = HERE / "devices.json"
    dv = json.loads(dv_p.read_text()) if dv_p.exists() else {}
    ip = a.ip or cfg.get("ip") or (next(iter(dv.values()), {}).get("ip") or "")
    region = a.region or cfg.get("region") or "US"
    if not ip:
        # sem IP ainda (ex.: primeiro boot do exe): abre o painel na tela
        # Conectar; o login→descoberta preenche devices.json/tray_config.
        logger.info("Sem IP salvo — iniciando no modo configurar")
    # persiste pro próximo boot
    cfg.update(ip=ip, region=region)
    cfg_p.write_text(json.dumps(cfg, indent=2))
    return ip, region


def main() -> None:
    logger.info("Starting web panel")
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip")
    ap.add_argument("--region")
    a = ap.parse_args()
    ip, region = load_settings(a)
    logger.info("Settings loaded; creating window")

    app = App(ip, region)
    logger.info("Application services initialized")
    api = Api(app)

    def on_shown():
        # O encaixe da janela NÃO passa pelo loop de eventos (deadlock:
        # evaluate_js espera a thread da UI). Ele vem do app.js fitPanel → Api.fit.
        app.submit(app.task_refresh())

    win = webview.create_window(
        "Ar Condicionado",
        str(RES / "ui" / "index.html"),
        js_api=api,
        width=412, height=624,
        x=_bottom_right_x(412), y=_bottom_left_y(624),
        frameless=True,
        transparent=True,
        easy_drag=False,             # drag via classe da UI (header)
        text_select=False,
    )
    app.win = win
    logger.info("Native window created; starting pywebview loop")

    def on_native_ready():
        """NA THREAD DA UI: só nela é seguro tocar win.native."""
        try:
            ntv = getattr(win, "native", None)
            _set_native_corners(getattr(ntv, "Handle", 0))
        except Exception:
            logger.exception("native window setup failed")

    win.events.shown += on_native_ready
    # Cabeçalho arrastável; o layout de painel é ativado em on_shown().
    _ADD_DRAG = ("document.querySelector('.head') && "
                 "document.querySelector('.head').classList.add('pywebview-drag-region')")
    try:
        win.events.loaded += lambda: win.evaluate_js(_ADD_DRAG)
    except Exception:                      # noqa: BLE001
        pass

    import pystray

    def quit_all(_=None, __=None) -> None:
        try:
            tasks = [t for t in asyncio.all_tasks(app.loop) if not t.done()]
            for t in tasks:
                t.cancel()
        except Exception:                  # noqa: BLE001
            pass
        app.loop.call_soon_threadsafe(app.loop.stop)
        try:
            icon.stop()
        except Exception:                  # noqa: BLE001
            pass
        try:
            webview.windows[0].destroy()
        except Exception:                  # noqa: BLE001
            pass

    icon = pystray.Icon("midea_ac_web", make_state_icon({}), "Ar Condicionado",
                        pystray.Menu(
                            pystray.MenuItem("Mostrar painel",
                                             lambda i, it: app.show_panel(), default=True),
                            pystray.MenuItem("Atualizar estado", lambda i, it: on_shown()),
                            pystray.MenuItem("Sair", quit_all),
                        ))

    def on_state_change(st) -> None:
        """Atualiza ícone + tooltip do bandeja com o estado confirmado."""
        try:
            icon.icon = make_state_icon(st)
            mode = _tray_mode_label(str(st.get("mode") or ""))
            icon.title = "AC {t}° · {m} · {p}".format(
                t=st.get("temp") if isinstance(st.get("temp"), (int, float)) else "--",
                m=mode, p="ligado" if st.get("power") else "desligado")
        except Exception:
            logger.exception("tray update failed")

    app.on_state_change = on_state_change
    threading.Thread(target=icon.run, daemon=True).start()

    webview.start(on_shown)
    logger.info("pywebview loop stopped")


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        logger.exception("Application startup failed")
