"""
Gemini Interpreter - interface web
----------------------------------
Installation :
    pip install flask playwright
    playwright install chromium

Lancement :
    python gemini_web.py
    -> ouvre http://127.0.0.1:5000 (variable d'environnement PORT pour changer)
"""

import os
import re
import sys
import time
import queue
import signal
import shutil
import secrets
import tempfile
import threading
import subprocess
import webbrowser

from pathlib import Path

from flask import Flask, Response, abort, jsonify, request
from werkzeug.utils import secure_filename
from playwright.sync_api import sync_playwright


GEMINI_URL = "https://gemini.google.com/app"
PROFILE_DIR = Path.home() / ".gemini_chrome_profile"
HOST = "127.0.0.1"
PORT = int(os.environ.get("PORT", "5000"))

SCRIPT_TIMEOUT = 120          # durée max d'un script (secondes)
RESPONSE_TIMEOUT = 180        # durée max d'attente d'une réponse Gemini
MAX_FEEDBACK_CHARS = 6000     # taille max de sortie renvoyée à Gemini
MAX_STEPS = 15                # nombre max d'étapes automatiques par demande
MAX_EVENTS = 3000             # historique gardé en mémoire pour l'interface

TOKEN = secrets.token_urlsafe(24)  # protège l'API contre les sites tiers

PRIME_PROMPT = (
    "Tu es un agent qui contrôle mon ordinateur Linux (Ubuntu) via un "
    "interpréteur de code, comme Open Interpreter.\n"
    "Règles :\n"
    "1. Pour agir, réponds avec UNE courte phrase puis UN SEUL bloc de code "
    "(```python ou ```bash).\n"
    "2. J'exécute le code et je te renvoie automatiquement la sortie.\n"
    "3. Continue étape par étape, en corrigeant les erreurs, jusqu'à ce que "
    "la tâche soit accomplie.\n"
    "4. Quand c'est terminé, réponds par un résumé SANS aucun bloc de code.\n"
    "5. Pas de placeholder, pas de demande de confirmation, pas d'input() "
    "(le script n'a pas de clavier). Le script s'exécute depuis mon dossier "
    "personnel.\n\n"
    "Ma demande : "
)

DANGEROUS_PATTERNS = [
    r"\brm\s+-[a-zA-Z]*[rf][a-zA-Z]*\s+(/|~|\$HOME|\*)",
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r":\(\)\s*\{",
    r"\b(shutdown|reboot|poweroff|halt)\b",
    r"\bsudo\b",
    r"chmod\s+-R\s+777\s+/",
    r">\s*/dev/sd",
    r"shutil\.rmtree",
]


# ======================================================================
# Moteur : navigateur Gemini + boucle agent + exécution de code
# ======================================================================

class Engine:
    def __init__(self):
        self.events = []
        self.next_id = 1
        self.ev_lock = threading.Lock()

        self.task_queue = queue.Queue()
        self.cancel_event = threading.Event()

        self.browser_ready = False
        self.closing = False

        self.auto = False
        self.primed = False
        self.steps = 0
        self.busy = False       # Gemini est en train de répondre
        self.running = False    # un script tourne
        self.danger_note = False
        self.status = "Initialisation du navigateur Gemini..."

        self.process_lock = threading.Lock()
        self.active_process = None
        self.execution_lock = threading.Lock()

        self.upload_dir = Path(tempfile.mkdtemp(prefix="gemini_uploads_"))

        self.chat("System", "Initialisation du navigateur Gemini...")
        self.browser_thread = threading.Thread(
            target=self.browser_worker, daemon=True
        )
        self.browser_thread.start()

    # ---------------- événements ----------------

    def emit(self, kind, **data):
        with self.ev_lock:
            self.events.append({"id": self.next_id, "kind": kind, **data})
            self.next_id += 1
            if len(self.events) > MAX_EVENTS:
                del self.events[:500]

    def events_since(self, since):
        with self.ev_lock:
            return [e for e in self.events if e["id"] > since]

    def chat(self, sender, text):
        self.emit("chat", sender=sender, text=text)

    def term(self, text):
        self.emit("term", text=text)

    def state(self):
        return {
            "browser_ready": self.browser_ready,
            "busy": self.busy,
            "running": self.running,
            "auto": self.auto,
            "status": self.status,
            "steps": self.steps,
            "max_steps": MAX_STEPS,
            "danger": self.danger_note,
        }

    # ---------------- actions utilisateur ----------------

    def send_user(self, question, file_path, display_name=None):
        text = question
        if display_name:
            text += f"\n[Fichier : {display_name}]"
        self.chat("Toi", text)

        self.cancel_event.clear()
        self.steps = 0
        self.danger_note = False
        self.busy = True
        self.status = "Gemini réfléchit..."
        self.task_queue.put(("msg", question, file_path, True))

    def new_conversation(self):
        self.stop()
        self.primed = False
        self.cancel_event.clear()
        self.task_queue.put(("new_chat",))
        self.chat("System", "Nouvelle conversation Gemini.")
        self.status = "Prêt."

    def stop(self):
        self.cancel_event.set()

        try:
            while True:
                self.task_queue.get_nowait()
        except queue.Empty:
            pass

        with self.process_lock:
            process = self.active_process
        if process is not None:
            self.terminate_process(process)

        self.busy = False
        self.status = "Arrêté."

    def close(self):
        if self.closing:
            return
        self.closing = True
        self.browser_ready = False
        self.cancel_event.set()
        self.task_queue.put(None)

        with self.process_lock:
            process = self.active_process
        if process is not None:
            self.terminate_process(process)

        self.browser_thread.join(timeout=10)
        shutil.rmtree(self.upload_dir, ignore_errors=True)

    # ---------------- navigateur Gemini ----------------

    def browser_worker(self):
        """
        Ouvre Gemini avec Google Chrome installé sur la machine,
        dans un profil séparé et persistant.

        Le profil dédié permet de conserver la session Google sans
        toucher au profil Chrome personnel de l'utilisateur.
        """
        context = None

        try:
            PROFILE_DIR.mkdir(parents=True, exist_ok=True)

            with sync_playwright() as p:
                # ------------------------------------------------------
                # Recherche de Google Chrome / Chromium système.
                # On privilégie le Chrome officiel afin d'éviter les
                # restrictions de connexion Google rencontrées avec
                # le Chromium livré par Playwright.
                # ------------------------------------------------------
                chrome_candidates = [
                    os.environ.get("GOOGLE_CHROME_PATH", ""),
                    "/usr/bin/google-chrome",
                    "/usr/bin/google-chrome-stable",
                    "/opt/google/chrome/google-chrome",
                    "/usr/bin/chromium",
                    "/usr/bin/chromium-browser",
                ]

                chrome_path = next(
                    (path for path in chrome_candidates
                     if path and os.path.isfile(path) and os.access(path, os.X_OK)),
                    None,
                )

                launch_options = {
                    "headless": True,
                    "viewport": None,
                    "args": [
                        "--start-maximized",
                        "--disable-blink-features=AutomationControlled",
                        "--no-first-run",
                        "--no-default-browser-check",
                    ],
                }

                if chrome_path:
                    launch_options["executable_path"] = chrome_path
                else:
                    # Si aucun binaire système n'est trouvé, Playwright
                    # essaie son canal Chrome officiel.
                    launch_options["channel"] = "chrome"

                context = p.chromium.launch_persistent_context(
                    str(PROFILE_DIR),
                    **launch_options,
                )

                page = (
                    context.pages[0]
                    if context.pages
                    else context.new_page()
                )

                # ------------------------------------------------------
                # Ouverture de Gemini
                # ------------------------------------------------------
                page.goto(
                    GEMINI_URL,
                    wait_until="domcontentloaded",
                    timeout=60000,
                )

                self.browser_ready = True
                self.status = "Prêt."

                self.chat(
                    "System",
                    "Google Chrome est ouvert. Connecte-toi à ton compte "
                    "Google si nécessaire. La session sera conservée dans "
                    "le profil Gemini dédié.",
                )

                # ------------------------------------------------------
                # Boucle principale du navigateur
                # ------------------------------------------------------
                while not self.closing:
                    try:
                        task = self.task_queue.get(timeout=0.5)
                    except queue.Empty:
                        continue

                    if task is None:
                        break

                    try:
                        if task[0] == "new_chat":
                            page.goto(
                                GEMINI_URL,
                                wait_until="domcontentloaded",
                                timeout=60000,
                            )

                        elif task[0] == "msg":
                            _, question, file_path, is_user = task
                            self.ask_gemini(
                                page,
                                question,
                                file_path,
                                is_user,
                            )

                    except Exception as exc:
                        self.busy = False
                        self.status = "Erreur."
                        self.chat(
                            "System",
                            f"Erreur lors de l'envoi : {exc}",
                        )

        except Exception as exc:
            self.chat(
                "System",
                f"Erreur critique navigateur : {exc}",
            )

        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass

            self.browser_ready = False

            if not self.closing:
                self.status = "Navigateur Gemini indisponible."
                self.chat(
                    "System",
                    "Le navigateur Gemini est indisponible.",
                )

    @staticmethod
    def find_input(page):
        selectors = [
            "div.ql-editor[contenteditable='true']",
            "div[role='textbox'][contenteditable='true']",
            "rich-textarea div[contenteditable='true']",
            "div[contenteditable='true']",
        ]
        for selector in selectors:
            try:
                locator = page.locator(selector).last
                if locator.is_visible(timeout=1000):
                    return locator
            except Exception:
                continue
        raise RuntimeError(
            "Champ de saisie introuvable. "
            "Vérifie la connexion et l'interface Gemini."
        )

    @staticmethod
    def get_responses(page):
        selectors = [
            "model-response",
            "message-content",
            ".model-response-text",
            ".response-container",
        ]
        for selector in selectors:
            try:
                texts = page.locator(selector).all_inner_texts()
                texts = [t.strip() for t in texts if t.strip()]
                if texts:
                    return texts
            except Exception:
                continue
        return []

    def attach_file(self, page, file_path):
        """
        Joint un fichier à Gemini.

        Gemini peut créer dynamiquement le <input type="file">.
        On essaie donc plusieurs méthodes, dans cet ordre :
        1) input[type=file] déjà présent ;
        2) bouton d'upload + file chooser Playwright ;
        3) bouton d'upload puis input[type=file] créé dynamiquement.
        """
        file_path = str(Path(file_path).resolve())

        if not Path(file_path).is_file():
            raise FileNotFoundError(f"Fichier introuvable : {file_path}")

        # --------------------------------------------------------------
        # Méthode 1 : input[type=file] déjà présent dans Gemini
        # --------------------------------------------------------------
        file_inputs = page.locator("input[type='file']")

        try:
            if file_inputs.count() > 0:
                file_inputs.last.set_input_files(
                    file_path,
                    timeout=30000
                )
                page.wait_for_timeout(2000)
                return
        except Exception:
            pass

        # --------------------------------------------------------------
        # Méthode 2/3 : bouton "Ajouter des fichiers" de Gemini
        # --------------------------------------------------------------
        upload_selectors = [
            'button[aria-label="Open upload file menu"]',
            'button[aria-label*="Upload" i]',
            'button[aria-label*="Attach" i]',
            'button[aria-label*="Add file" i]',
            'button[aria-label*="Add files" i]',
            '[role="button"][aria-label*="Upload" i]',
            '[role="button"][aria-label*="Attach" i]',
            '[role="button"][aria-label*="Add file" i]',
            '[role="button"][aria-label*="Add files" i]',
        ]

        last_error = None

        for selector in upload_selectors:
            try:
                buttons = page.locator(selector)
                count = buttons.count()

                for index in range(count - 1, -1, -1):
                    button = buttons.nth(index)

                    if not button.is_visible(timeout=1000):
                        continue

                    # Le bouton peut ouvrir directement le sélecteur
                    # de fichiers.
                    try:
                        with page.expect_file_chooser(timeout=5000) as fc_info:
                            button.click(timeout=5000)

                        file_chooser = fc_info.value
                        file_chooser.set_files(file_path)
                        page.wait_for_timeout(2000)
                        return

                    except Exception as exc:
                        last_error = exc

                    # Sinon, le bouton ouvre d'abord le menu Gemini.
                    page.wait_for_timeout(500)

                    menu_selectors = [
                        '[role="menuitem"]',
                        'button[role="menuitem"]',
                        '[role="option"]',
                    ]

                    for menu_selector in menu_selectors:
                        try:
                            menu_items = page.locator(menu_selector)
                            menu_count = menu_items.count()

                            for menu_index in range(menu_count - 1, -1, -1):
                                item = menu_items.nth(menu_index)

                                if not item.is_visible(timeout=500):
                                    continue

                                label = (
                                    item.get_attribute("aria-label") or ""
                                ).lower()

                                try:
                                    item_text = item.inner_text(
                                        timeout=1000
                                    ).lower()
                                except Exception:
                                    item_text = ""

                                combined = f"{label} {item_text}"

                                if not any(word in combined for word in (
                                    "upload",
                                    "fichier",
                                    "file",
                                    "files",
                                    "import",
                                )):
                                    continue

                                try:
                                    with page.expect_file_chooser(
                                        timeout=5000
                                    ) as fc_info:
                                        item.click(timeout=5000)

                                    file_chooser = fc_info.value
                                    file_chooser.set_files(file_path)
                                    page.wait_for_timeout(2000)
                                    return

                                except Exception as exc:
                                    last_error = exc

                        except Exception as exc:
                            last_error = exc

                    # Après l'ouverture du menu, Gemini peut avoir créé
                    # dynamiquement son input file.
                    try:
                        page.wait_for_timeout(500)
                        file_inputs = page.locator("input[type='file']")

                        if file_inputs.count() > 0:
                            file_inputs.last.set_input_files(
                                file_path,
                                timeout=30000
                            )
                            page.wait_for_timeout(2000)
                            return
                    except Exception as exc:
                        last_error = exc

            except Exception as exc:
                last_error = exc

        if last_error is not None:
            raise RuntimeError(
                "Gemini n'a pas permis d'ouvrir le sélecteur de fichier. "
                f"Détail : {last_error}"
            )

        raise RuntimeError(
            "Bouton d'ajout de fichier introuvable dans Gemini."
        )

    def ask_gemini(self, page, question, file_path, is_user):
        if file_path:
            try:
                self.attach_file(page, file_path)
            except Exception as exc:
                self.chat(
                    "System",
                    f"Impossible de joindre le fichier : {exc}"
                )
                self.busy = False
                self.status = "Prêt."
                return

        input_box = self.find_input(page)

        old_responses = self.get_responses(page)
        old_last = old_responses[-1] if old_responses else ""

        full_question = question
        if is_user and not self.primed:
            full_question = PRIME_PROMPT + question
            self.primed = True

        input_box.fill(full_question)
        input_box.press("Enter")

        deadline = time.monotonic() + RESPONSE_TIMEOUT
        last_text = ""
        stable_since = None

        while time.monotonic() < deadline:
            if self.closing or self.cancel_event.is_set():
                return

            page.wait_for_timeout(500)
            responses = self.get_responses(page)
            if not responses:
                continue

            candidate = responses[-1].strip()
            if not candidate or candidate == old_last:
                continue

            if candidate == last_text:
                if stable_since is None:
                    stable_since = time.monotonic()
                elif time.monotonic() - stable_since >= 3:
                    break
            else:
                last_text = candidate
                stable_since = time.monotonic()

        if self.cancel_event.is_set():
            return

        if not last_text:
            self.busy = False
            self.status = "Prêt."
            self.chat(
                "System",
                "Aucune nouvelle réponse détectée. "
                "Vérifie le navigateur puis réessaie.",
            )
            return

        self.chat("Gemini", last_text)

        lang, code = self.extract_code(page, last_text)
        self.on_gemini_reply(lang, code)

    @staticmethod
    def extract_code(page, fallback_text):
        """Extrait le code de la DERNIÈRE réponse uniquement."""
        lang, code = "", ""

        try:
            response = page.locator("model-response").last
            blocks = response.locator("code-block").all()

            if blocks:
                block = blocks[-1]

                lang_elem = block.locator(
                    ".code-block-decoration span, [class*='header'] span"
                ).first
                if lang_elem.count() > 0:
                    lang = lang_elem.inner_text().strip().lower()

                code_elem = block.locator("code").first
                if code_elem.count() > 0:
                    code = code_elem.inner_text().strip()
        except Exception:
            pass

        if not code and fallback_text:
            pattern = re.compile(
                r"```([a-zA-Z0-9_+#.-]*)\s*\n?(.*?)```", re.DOTALL
            )
            matches = pattern.findall(fallback_text)
            if matches:
                lang, code = matches[-1]
                lang, code = lang.strip().lower(), code.strip()

        if not code:
            return "", ""

        if any(x in lang for x in ("bash", "shell", "zsh")) or lang == "sh":
            return "bash", code
        if "python" in lang or lang == "py":
            return "python", code

        lines = code.splitlines()
        first_line = lines[0] if lines else ""
        if first_line.startswith("#!") and ("bash" in first_line or "sh" in first_line):
            return "bash", code

        return "python", code

    # ---------------- boucle agent ----------------

    @staticmethod
    def is_dangerous(code):
        return any(re.search(p, code) for p in DANGEROUS_PATTERNS)

    def on_gemini_reply(self, lang, code):
        if self.cancel_event.is_set():
            return

        if not code:
            self.busy = False
            self.status = "Terminé."
            self.chat("System", "Aucun nouveau code : la tâche semble terminée.")
            return

        self.emit("code", lang=lang or "python", code=code)
        self.busy = False

        if self.steps >= MAX_STEPS:
            self.status = "Limite d'étapes atteinte."
            self.chat(
                "System",
                f"Limite de {MAX_STEPS} étapes atteinte. Relis le code puis "
                "clique sur Exécuter pour continuer.",
            )
            return

        dangerous = self.is_dangerous(code)

        if self.auto and not dangerous:
            self.start_execution(code, lang)
            return

        if dangerous and self.auto:
            self.danger_note = True
            self.chat(
                "System",
                "Commande potentiellement dangereuse détectée : "
                "confirmation requise.",
            )
        else:
            self.chat("System", "Code transféré dans l'éditeur.")

        self.status = "Code prêt : clique sur « Exécuter »."

    def send_feedback(self, lang, return_code, output):
        if self.closing or self.cancel_event.is_set():
            return

        self.steps += 1

        output = output.strip() or "(aucune sortie)"
        if len(output) > MAX_FEEDBACK_CHARS:
            output = "[...tronqué...]\n" + output[-MAX_FEEDBACK_CHARS:]

        status = (
            "réussi (code 0)" if return_code == 0 else f"ÉCHOUÉ (code {return_code})"
        )

        prompt = (
            f"Résultat de l'exécution du script {lang.upper()} : {status}.\n"
            f"Sortie :\n{output}\n\n"
            "Si la tâche est accomplie, réponds par un résumé SANS bloc de "
            "code. Sinon, envoie le prochain bloc de code "
            "(corrigé si nécessaire)."
        )

        self.chat(
            "System",
            f"Résultat envoyé à Gemini ({status}). "
            f"Étape {self.steps}/{MAX_STEPS}.",
        )
        self.busy = True
        self.status = "Gemini réfléchit..."
        self.task_queue.put(("msg", prompt, None, False))

    # ---------------- exécution ----------------

    def start_execution(self, code, lang):
        if not self.execution_lock.acquire(blocking=False):
            return False

        self.running = True
        self.danger_note = False
        self.status = f"Exécution {lang.upper()} en cours..."

        threading.Thread(
            target=self.run_script_live, args=(code, lang), daemon=True
        ).start()
        return True

    def run_script_live(self, code, lang):
        filename = None
        process = None
        collected = []
        return_code = -1

        def out(text):
            collected.append(text)
            self.term(text)

        try:
            suffix = ".sh" if lang == "bash" else ".py"

            with tempfile.NamedTemporaryFile(
                mode="w", suffix=suffix, encoding="utf-8", delete=False
            ) as temp:
                temp.write(code)
                filename = temp.name

            if lang == "bash":
                command = [shutil.which("bash") or "bash", filename]
            else:
                command = [sys.executable, "-u", filename]

            out(f"\n--- Lancement {lang.upper()} ---\n")

            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=True,
                cwd=str(Path.home()),
            )

            with self.process_lock:
                self.active_process = process

            output_queue = queue.Queue()

            def read_output():
                try:
                    for line in process.stdout:
                        output_queue.put(line)
                finally:
                    output_queue.put(None)

            threading.Thread(target=read_output, daemon=True).start()

            deadline = time.monotonic() + SCRIPT_TIMEOUT
            output_finished = False

            while True:
                if self.closing:
                    self.terminate_process(process)
                    break

                if time.monotonic() >= deadline:
                    out("\nDélai maximal dépassé. Arrêt demandé.\n")
                    self.terminate_process(process)
                    break

                try:
                    item = output_queue.get(timeout=0.1)
                except queue.Empty:
                    item = ""

                if item is None:
                    output_finished = True
                elif item:
                    out(item)

                if process.poll() is not None and output_finished:
                    break

            try:
                return_code = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.terminate_process(process)
                return_code = process.poll()

            if return_code is None:
                return_code = -1

            out(f"\n--- Fin du script (code : {return_code}) ---\n")

        except Exception as exc:
            return_code = -1
            out(f"\nErreur d'exécution : {exc}\n")
            if process is not None:
                self.terminate_process(process)

        finally:
            with self.process_lock:
                self.active_process = None

            if filename:
                try:
                    os.unlink(filename)
                except OSError:
                    pass

            self.running = False
            self.execution_lock.release()

        # Succès ou échec : on renvoie toujours le résultat à Gemini
        if not self.closing and not self.cancel_event.is_set():
            self.send_feedback(lang, return_code, "".join(collected))
        elif self.cancel_event.is_set():
            self.status = "Arrêté."

    @staticmethod
    def terminate_process(process):
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        except ProcessLookupError:
            pass
        except OSError:
            try:
                process.kill()
            except OSError:
                pass


# ======================================================================
# Serveur web
# ======================================================================

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024
engine = None


@app.before_request
def guard():
    # Anti DNS-rebinding : seules ces adresses sont acceptées
    host = request.host.rsplit(":", 1)[0]
    if host not in ("127.0.0.1", "localhost"):
        abort(403)

    # Anti requêtes depuis un autre site : jeton obligatoire pour l'API
    if request.path.startswith("/api/"):
        given = request.headers.get("X-Token", "")
        if not secrets.compare_digest(given, TOKEN):
            abort(403)


@app.get("/")
def index():
    return Response(PAGE.replace("__TOKEN__", TOKEN), mimetype="text/html")


@app.get("/api/events")
def api_events():
    since = request.args.get("since", "0")
    since = int(since) if since.isdigit() else 0
    return jsonify(events=engine.events_since(since), state=engine.state())


@app.post("/api/send")
def api_send():
    text = (request.form.get("text") or "").strip()
    upload = request.files.get("file")

    path = None
    display_name = None

    if upload and upload.filename:
        display_name = secure_filename(upload.filename) or "fichier"
        path = str(engine.upload_dir / f"{secrets.token_hex(4)}_{display_name}")
        upload.save(path)

    if not text and not path:
        return jsonify(error="Message vide."), 400

    if not engine.browser_ready:
        return jsonify(error="Gemini n'est pas encore prêt."), 409

    engine.send_user(text or "Regarde le fichier joint.", path, display_name)
    return jsonify(ok=True)


@app.post("/api/run")
def api_run():
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip()
    lang = data.get("lang")
    confirmed = bool(data.get("confirmed"))

    if not code:
        return jsonify(error="L'éditeur est vide."), 400
    if lang not in ("python", "bash"):
        return jsonify(error="Langage invalide."), 400

    dangerous = engine.is_dangerous(code)
    if not confirmed and not (engine.auto and not dangerous):
        return jsonify(error="Confirmation requise.", need_confirm=True), 409

    engine.cancel_event.clear()
    if not engine.start_execution(code, lang):
        return jsonify(error="Un script est déjà en cours d'exécution."), 409
    return jsonify(ok=True)


@app.post("/api/stop")
def api_stop():
    engine.stop()
    return jsonify(ok=True)


@app.post("/api/new")
def api_new():
    engine.new_conversation()
    return jsonify(ok=True)


@app.post("/api/auto")
def api_auto():
    data = request.get_json(silent=True) or {}
    engine.auto = bool(data.get("value"))
    return jsonify(auto=engine.auto)


# ======================================================================
# Page web (HTML / CSS / JS)
# ======================================================================

PAGE = r"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gemini Interpreter</title>
<style>
:root{
  --bg:#000;
  --sidebar:#171717;
  --sidebar-hover:#232323;
  --panel:#202020;
  --panel-2:#2a2a2a;
  --text:#f5f5f5;
  --muted:#a7a7a7;
  --border:#303030;
  --accent:#fff;
  --accent-ink:#111;
  --danger:#ef4444;
  --ok:#4ade80;
  --term-bg:#0d0d0d;
  --term-ink:#e5e5e5;
  --font:Inter,ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  --mono:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,"Liberation Mono",monospace;
}
*{box-sizing:border-box}
html,body{height:100%;margin:0}
body{
  background:var(--bg);color:var(--text);font:14px/1.5 var(--font);
  display:flex;overflow:hidden;
}
button,input,textarea,select{font:inherit}
button{border:0}
:focus-visible{outline:2px solid #777;outline-offset:2px}

/* ===== Barre latérale ===== */
.sidebar{
  width:260px;flex:0 0 260px;background:var(--sidebar);
  display:flex;flex-direction:column;padding:12px 10px;
  border-right:1px solid #222;
}
.brand{
  display:flex;align-items:center;gap:10px;padding:10px 12px 16px;
  font-size:16px;font-weight:650;
}
.brand-mark{
  width:28px;height:28px;border-radius:9px;background:#fff;color:#111;
  display:grid;place-items:center;font-size:15px;font-weight:800;
}
.new-chat{
  width:100%;display:flex;align-items:center;gap:10px;
  background:transparent;color:var(--text);padding:11px 12px;
  border-radius:9px;cursor:pointer;text-align:left;
}
.new-chat:hover{background:var(--sidebar-hover)}
.new-chat .plus{font-size:22px;font-weight:300;line-height:1}
.side-label{
  padding:20px 12px 7px;color:#777;font-size:11px;font-weight:650;
  text-transform:uppercase;letter-spacing:.08em;
}
.side-item{
  display:flex;align-items:center;gap:10px;width:100%;
  color:#cfcfcf;background:transparent;padding:9px 12px;
  border-radius:8px;cursor:pointer;text-align:left;
}
.side-item:hover,.side-item.active{background:var(--sidebar-hover);color:#fff}
.side-icon{width:18px;text-align:center;color:#aaa}
.history{
  flex:1;overflow:auto;padding-top:2px;
}
.history::-webkit-scrollbar,.chat::-webkit-scrollbar{width:7px}
.history::-webkit-scrollbar-thumb,.chat::-webkit-scrollbar-thumb{
  background:#3b3b3b;border-radius:10px
}
.history-item{
  display:block;width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  color:#aaa;background:transparent;padding:8px 12px;border-radius:8px;
  text-align:left;cursor:pointer;font-size:13px;
}
.history-item:hover{background:#222;color:#eee}
.account{
  border-top:1px solid #292929;padding:10px 4px 0;margin-top:8px;
}
.account-btn{
  width:100%;display:flex;align-items:center;gap:10px;padding:9px 8px;
  background:transparent;color:#ddd;border-radius:8px;cursor:pointer;
}
.account-btn:hover{background:#232323}
.avatar{
  width:30px;height:30px;border-radius:50%;background:#2f8f78;color:#fff;
  display:grid;place-items:center;font-size:12px;font-weight:700;
}
.account-text{min-width:0;flex:1;text-align:left}
.account-name{font-size:13px;font-weight:600}
.account-plan{font-size:11px;color:#888}

/* ===== Zone principale ===== */
.app{
  min-width:0;flex:1;height:100%;display:flex;flex-direction:column;
  position:relative;background:#000;
}
.topbar{
  height:56px;flex:0 0 56px;display:flex;align-items:center;
  padding:0 20px;border-bottom:1px solid #151515;
}
.top-title{font-size:15px;font-weight:600}
.top-spacer{flex:1}
.status{
  display:flex;align-items:center;gap:7px;color:#888;font-size:12px;margin-right:12px;
}
.dot{width:7px;height:7px;border-radius:50%;background:#666}
.dot.busy{background:#fff;animation:pulse 1.2s ease-in-out infinite}
.dot.ok{background:var(--ok)}
.dot.err{background:var(--danger)}
@keyframes pulse{50%{opacity:.25}}
.top-actions{display:flex;align-items:center;gap:4px}
.icon-btn{
  width:36px;height:36px;border-radius:8px;background:transparent;color:#aaa;
  cursor:pointer;display:grid;place-items:center;font-size:17px;
}
.icon-btn:hover{background:#1d1d1d;color:#fff}
.icon-btn.active{background:#252525;color:#fff}

/* ===== Conversation ===== */
.workspace{
  min-height:0;flex:1;position:relative;display:flex;flex-direction:column;
}
.chat-wrap{
  min-height:0;flex:1;width:min(920px,calc(100% - 40px));margin:0 auto;
  display:flex;flex-direction:column;
}
.chat{
  flex:1;overflow-y:auto;padding:36px 0 28px;
}
.welcome{
  flex:1;display:flex;flex-direction:column;align-items:center;
  justify-content:center;text-align:center;padding:30px 20px 50px;
}
.welcome h1{font-size:30px;line-height:1.2;font-weight:650;margin:0 0 12px;letter-spacing:-.02em}
.welcome p{margin:0;color:#8f8f8f;font-size:14px}
.msg{
  display:flex;gap:13px;margin:0 auto 25px;max-width:820px;
}
.msg .who{display:none}
.msg .body{
  white-space:pre-wrap;overflow-wrap:anywhere;flex:1;min-width:0;
}
.msg.Toi{justify-content:flex-end}
.msg.Toi .body{
  flex:0 1 auto;max-width:78%;background:#2a2a2a;
  padding:10px 14px;border-radius:18px;
}
.msg.Gemini .body{padding-top:3px}
.msg.System{
  color:#777;font-size:12px;justify-content:center;
}
.msg.System .body{font-size:12px;color:#777;text-align:center}

/* ===== Composer ===== */
.composer-area{
  width:min(920px,calc(100% - 40px));margin:0 auto;padding:0 0 22px;
}
.composer{
  position:relative;background:#2a2a2a;border:1px solid #3a3a3a;
  border-radius:25px;padding:12px 12px 9px;box-shadow:0 1px 3px rgba(0,0,0,.35);
}
.composer textarea{
  display:block;width:100%;min-height:30px;max-height:180px;
  resize:none;overflow-y:auto;border:0;outline:0;background:transparent;
  color:#f5f5f5;padding:1px 7px 8px;font:15px/1.45 var(--font);
}
.composer textarea::placeholder{color:#858585}
.composer-row{display:flex;align-items:center;gap:5px}
.attach-btn,.tool-btn{
  height:34px;min-width:34px;padding:0 9px;border-radius:17px;
  background:transparent;color:#bdbdbd;cursor:pointer;display:flex;
  align-items:center;justify-content:center;gap:6px;
}
.attach-btn:hover,.tool-btn:hover{background:#363636;color:#fff}
.file-name{
  max-width:260px;color:#aaa;font-size:12px;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap;
}
.composer-spacer{flex:1}
.send-btn{
  width:34px;height:34px;border-radius:50%;background:#fff;color:#111;
  display:grid;place-items:center;cursor:pointer;font-size:16px;font-weight:700;
}
.send-btn:hover{background:#ddd}
.send-btn:disabled{opacity:.35;cursor:not-allowed}
.composer-hint{
  text-align:center;color:#666;font-size:11px;margin-top:8px;
}

/* ===== Panneaux utilitaires ===== */
.drawer{
  position:absolute;z-index:20;top:0;right:0;bottom:0;width:min(620px,92vw);
  background:#171717;border-left:1px solid #303030;box-shadow:-18px 0 45px rgba(0,0,0,.45);
  display:flex;flex-direction:column;transform:translateX(105%);
  transition:transform .18s ease;
}
.drawer.open{transform:translateX(0)}
.drawer-head{
  height:56px;display:flex;align-items:center;padding:0 14px;
  border-bottom:1px solid #292929;
}
.drawer-title{font-weight:650}
.drawer-close{
  margin-left:auto;width:34px;height:34px;border-radius:8px;
  background:transparent;color:#aaa;cursor:pointer;font-size:18px;
}
.drawer-close:hover{background:#252525;color:#fff}
.drawer-body{flex:1;min-height:0;display:flex;flex-direction:column}
#code{
  flex:1;min-height:0;margin:0;padding:18px;
  border:0;outline:0;resize:none;background:#111;color:#eee;
  font:13px/1.6 var(--mono);tab-size:4;white-space:pre;
}
.drawer-toolbar{
  display:flex;align-items:center;gap:8px;padding:10px;border-bottom:1px solid #292929;
}
.drawer-toolbar select{
  background:#222;color:#ddd;border:1px solid #3a3a3a;border-radius:8px;padding:7px 9px;
}
.btn{
  border:1px solid #3a3a3a;background:#242424;color:#ddd;
  border-radius:8px;padding:7px 11px;cursor:pointer;
}
.btn:hover:not(:disabled){background:#303030;color:#fff}
.btn.primary{background:#fff;color:#111;border-color:#fff}
.btn.danger{background:#5b2020;color:#fff;border-color:#733030}
.btn:disabled{opacity:.4;cursor:not-allowed}
.banner{
  display:none;margin:10px;padding:9px 11px;border-radius:8px;
  border:1px solid #7d3030;color:#ff8585;background:#251515;font-size:12px;
}
.banner.show{display:block}
#term{
  flex:1;min-height:0;margin:0;padding:16px;overflow:auto;
  white-space:pre-wrap;overflow-wrap:anywhere;background:var(--term-bg);
  color:var(--term-ink);font:12.5px/1.55 var(--mono);
}
.drawer-overlay{
  display:none;position:absolute;z-index:19;inset:0;background:rgba(0,0,0,.42);
}
.drawer-overlay.show{display:block}

@media(max-width:800px){
  .sidebar{width:72px;flex-basis:72px;padding:10px 7px}
  .brand{justify-content:center;padding:8px}
  .brand span,.new-chat span,.side-item span:not(.side-icon),
  .side-label,.history,.account-text{display:none}
  .new-chat,.side-item{justify-content:center;padding:10px}
  .account-btn{justify-content:center}
  .chat-wrap,.composer-area{width:calc(100% - 24px)}
  .welcome h1{font-size:25px}
}
</style>
</head>
<body>

<aside class="sidebar">
  <div class="brand">
    <div class="brand-mark">G</div>
    <span>Gemini Interpreter</span>
  </div>

  <button class="new-chat" id="btn-new">
    <span class="plus">+</span><span>Nouvelle conversation</span>
  </button>

  <div class="side-label">Espace de travail</div>
  <button class="side-item active" id="side-chat">
    <span class="side-icon">◌</span><span>Discussion</span>
  </button>
  <button class="side-item" id="side-code">
    <span class="side-icon">&lt;/&gt;</span><span>Code</span>
  </button>
  <button class="side-item" id="side-term">
    <span class="side-icon">›_</span><span>Terminal</span>
  </button>

  <div class="side-label">Contrôle</div>
  <button class="side-item" id="side-auto" title="Active ou désactive le mode automatique">
    <span class="side-icon">⚙</span><span>Mode automatique</span>
    <input type="checkbox" id="auto" style="margin-left:auto;accent-color:#fff">
  </button>

  <div class="history">
    <div class="side-label">Récents</div>
    <button class="history-item">Gemini Interpreter</button>
    <button class="history-item">Nouvelle conversation</button>
  </div>

  <div class="account">
    <button class="account-btn">
      <div class="avatar">U</div>
      <div class="account-text">
        <div class="account-name">Utilisateur</div>
        <div class="account-plan">Gemini Interpreter</div>
      </div>
      <span style="color:#777">•••</span>
    </button>
  </div>
</aside>

<main class="app">
  <header class="topbar">
    <div class="top-title">Gemini Interpreter</div>
    <div class="top-spacer"></div>
    <div class="status" role="status" aria-live="polite">
      <span class="dot" id="dot"></span>
      <span id="status">Connexion...</span>
    </div>
    <div class="top-actions">
      <button class="icon-btn" id="btn-code-top" title="Afficher le code">&lt;/&gt;</button>
      <button class="icon-btn" id="btn-term-top" title="Afficher le terminal">›_</button>
      <button class="icon-btn" id="btn-stop" title="Arrêter">■</button>
    </div>
  </header>

  <section class="workspace">
    <div class="chat-wrap">
      <div id="chat" class="chat" aria-live="polite">
        <div class="welcome" id="welcome">
          <h1>Que veux-tu faire aujourd'hui ?</h1>
          <p>Décris une tâche et Gemini pourra travailler étape par étape avec ton ordinateur.</p>
        </div>
      </div>
    </div>

    <div class="composer-area">
      <div class="composer">
        <textarea id="msg" rows="1" placeholder="Décris la tâche à réaliser..."></textarea>
        <div class="composer-row">
          <input type="file" id="file" hidden>
          <button class="attach-btn" id="btn-attach" title="Joindre un fichier">＋</button>
          <span class="file-name" id="file-name"></span>
          <span class="composer-spacer"></span>
          <button class="tool-btn" id="btn-auto-mobile" title="Mode automatique">⚙</button>
          <button class="send-btn" id="btn-send" title="Envoyer">↑</button>
        </div>
      </div>
      <div class="composer-hint">Gemini Interpreter peut exécuter du code sur ton ordinateur. Vérifie toujours le code avant de l'autoriser.</div>
    </div>

    <div class="drawer-overlay" id="drawer-overlay"></div>

    <section class="drawer" id="code-drawer">
      <div class="drawer-head">
        <div class="drawer-title">Code à exécuter</div>
        <button class="drawer-close" data-close="code">×</button>
      </div>
      <div class="drawer-toolbar">
        <select id="lang" aria-label="Langage">
          <option value="python">Python</option>
          <option value="bash">Bash</option>
        </select>
        <span style="flex:1"></span>
        <button class="btn primary" id="btn-run">Exécuter</button>
      </div>
      <div class="banner" id="danger">Commande potentiellement dangereuse : relis le code avant d'exécuter.</div>
      <div class="drawer-body">
        <textarea id="code" spellcheck="false" placeholder="Le code proposé par Gemini apparaît ici. Tu peux le modifier avant de l'exécuter."></textarea>
      </div>
    </section>

    <section class="drawer" id="term-drawer">
      <div class="drawer-head">
        <div class="drawer-title">Terminal</div>
        <button class="drawer-close" data-close="term">×</button>
      </div>
      <div class="drawer-body">
        <pre id="term"></pre>
      </div>
    </section>
  </section>
</main>

<script>
const TOKEN = "__TOKEN__";
const $ = s => document.querySelector(s);
const JSON_HEADERS = { "Content-Type": "application/json" };
let lastId = 0;
let attached = null;

async function api(path, opts = {}) {
  opts.headers = Object.assign({ "X-Token": TOKEN }, opts.headers || {});
  const r = await fetch(path, opts);
  let data = {};
  try { data = await r.json(); } catch (e) {}
  if (!r.ok) {
    const err = new Error(data.error || ("Erreur " + r.status));
    err.needConfirm = !!data.need_confirm;
    throw err;
  }
  return data;
}

function nearBottom(el) {
  return el.scrollHeight - el.scrollTop - el.clientHeight < 60;
}

function addChat(sender, text) {
  const chat = $("#chat");
  const welcome = $("#welcome");
  if (welcome) welcome.remove();

  const stick = nearBottom(chat);
  const box = document.createElement("div");
  box.className = "msg " + sender;
  const who = document.createElement("div");
  who.className = "who";
  who.textContent = { Toi: "Toi", Gemini: "Gemini", System: "Système" }[sender] || sender;
  const body = document.createElement("div");
  body.className = "body";
  body.textContent = text;
  box.append(who, body);
  chat.append(box);
  if (stick) chat.scrollTop = chat.scrollHeight;
}

function addTerm(text) {
  const el = $("#term");
  const stick = nearBottom(el);
  el.append(document.createTextNode(text));
  while (el.childNodes.length > 4000) el.removeChild(el.firstChild);
  if (stick) el.scrollTop = el.scrollHeight;
}

function applyState(s) {
  $("#status").textContent = s.status;
  const dot = $("#dot");
  dot.className = "dot " + (!s.browser_ready ? "err" : (s.busy || s.running) ? "busy" : "ok");

  $("#auto").checked = s.auto;
  $("#danger").classList.toggle("show", s.danger);
  $("#btn-send").disabled = !s.browser_ready;
  $("#btn-run").disabled = s.running;
}

async function poll() {
  try {
    const d = await api("/api/events?since=" + lastId);
    for (const e of d.events) {
      lastId = e.id;
      if (e.kind === "chat") addChat(e.sender, e.text);
      else if (e.kind === "term") addTerm(e.text);
      else if (e.kind === "code") {
        $("#code").value = e.code;
        $("#lang").value = e.lang;
        openDrawer("code");
      }
    }
    applyState(d.state);
  } catch (err) {
    $("#status").textContent = "Connexion au serveur perdue...";
    $("#dot").className = "dot err";
  }
  setTimeout(poll, 600);
}

function setAttached(file) {
  attached = file;
  $("#file-name").textContent = file ? file.name : "";
}

async function send() {
  const text = $("#msg").value.trim();
  if (!text && !attached) return;
  const fd = new FormData();
  fd.append("text", text);
  if (attached) fd.append("file", attached);
  try {
    await api("/api/send", { method: "POST", body: fd });
    $("#msg").value = "";
    setAttached(null);
    $("#file").value = "";
  } catch (e) { alert(e.message); }
}

async function run() {
  const code = $("#code").value.trim();
  const lang = $("#lang").value;
  if (!code) { alert("L'éditeur est vide."); return; }

  const auto = $("#auto").checked;
  const post = confirmed => api("/api/run", {
    method: "POST", headers: JSON_HEADERS,
    body: JSON.stringify({ code, lang, confirmed })
  });

  try {
    if (!auto) {
      const ok = confirm("Exécuter ce script " + lang.toUpperCase() + " ?\n\n" +
        "Il aura accès aux fichiers et aux droits de ton compte Linux. " +
        "Vérifie le code avant de continuer.");
      if (!ok) return;
      await post(true);
      return;
    }
    try {
      await post(false);
    } catch (e) {
      if (!e.needConfirm) throw e;
      if (confirm("Commande potentiellement dangereuse. Exécuter quand même ?")) {
        await post(true);
      }
    }
  } catch (e) { alert(e.message); }
}

function openDrawer(which) {
  $("#drawer-overlay").classList.add("show");
  $("#code-drawer").classList.toggle("open", which === "code");
  $("#term-drawer").classList.toggle("open", which === "term");
  $("#side-code").classList.toggle("active", which === "code");
  $("#side-term").classList.toggle("active", which === "term");
  $("#side-chat").classList.toggle("active", false);
}
function closeDrawers() {
  $("#drawer-overlay").classList.remove("show");
  $("#code-drawer").classList.remove("open");
  $("#term-drawer").classList.remove("open");
  $("#side-code").classList.remove("active");
  $("#side-term").classList.remove("active");
  $("#side-chat").classList.add("active");
}

$("#btn-send").addEventListener("click", send);
$("#msg").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
$("#msg").addEventListener("input", e => {
  e.target.style.height = "auto";
  e.target.style.height = Math.min(e.target.scrollHeight, 180) + "px";
});
$("#btn-attach").addEventListener("click", () => $("#file").click());
$("#file").addEventListener("change", e => setAttached(e.target.files[0] || null));
$("#btn-run").addEventListener("click", run);
$("#btn-stop").addEventListener("click", () => api("/api/stop", { method: "POST" }).catch(e => alert(e.message)));
$("#btn-new").addEventListener("click", () => api("/api/new", { method: "POST" }).catch(e => alert(e.message)));

$("#side-code").addEventListener("click", () => openDrawer("code"));
$("#btn-code-top").addEventListener("click", () => openDrawer("code"));
$("#side-term").addEventListener("click", () => openDrawer("term"));
$("#btn-term-top").addEventListener("click", () => openDrawer("term"));
$("#side-chat").addEventListener("click", closeDrawers);
$("#drawer-overlay").addEventListener("click", closeDrawers);
document.querySelectorAll(".drawer-close").forEach(b => b.addEventListener("click", closeDrawers));

async function toggleAuto(ev) {
  const on = ev.target.checked;
  if (on && !confirm("Gemini pourra exécuter du code sur ton PC SANS ta validation, " +
      "avec les droits de ton compte.\n\nLes commandes jugées dangereuses " +
      "(rm -rf, sudo, dd...) demanderont quand même confirmation.\n\nActiver le mode auto ?")) {
    ev.target.checked = false;
    return;
  }
  try {
    await api("/api/auto", {
      method: "POST", headers: JSON_HEADERS,
      body: JSON.stringify({ value: on })
    });
  } catch (e) {
    alert(e.message);
    ev.target.checked = !on;
  }
}
$("#auto").addEventListener("change", toggleAuto);
$("#btn-auto-mobile").addEventListener("click", () => {
  const a = $("#auto");
  a.checked = !a.checked;
  a.dispatchEvent(new Event("change"));
});

poll();
</script>
</body>
</html>
"""

# ======================================================================

def main():
    global engine
    engine = Engine()

    url = f"http://{HOST}:{PORT}"
    print(f"Interface web : {url}")
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    try:
        app.run(host=HOST, port=PORT, threaded=True, debug=False, use_reloader=False)
    finally:
        engine.close()


if __name__ == "__main__":
    main()