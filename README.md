# Orchestrator

Egyszerű orchestrator kódoló agentek (Claude Code, Codex) vezérlésére. Egy orchestrator
agenttel beszélsz; ő projekteket, projekt-lead agenteket és azok alatt worktree-ben dolgozó
worker agenteket irányít.

```
te ── orchestrator (interaktív Claude Code session, `orch chat`)
        └─ projekt ── lead agent (a projekt fő checkoutjában, saját al-agenteket indíthat)
                        ├─ worker    (saját git worktree + `orch/<id>` branch)
                        ├─ reviewer  (a worker worktree-jében, utána fut)
                        └─ ...
```

## Telepítés

Kell hozzá: Python 3.14, `uv`, `git`, valamint a `claude` és/vagy a `codex` CLI bejelentkezve.

```sh
uv tool install --editable .     # az `orch` parancs felkerül a PATH-ra
# vagy telepítés nélkül, a repóból:  uv run orch ...
```

## Használat

```sh
orch project add ~/Projects/valami        # git repó regisztrálása projektként
orch chat                                 # az orchestrator megnyitása (Claude Code)
orch web                                  # webes felület: http://127.0.0.1:8787
```

### Webes felület

Az `orch web` felületéről minden irányítható:

- **Orchestrator** (bal felül): beszélgetés a legfelső szintű orchestrator agenttel, amely
  projekteket regisztrál, lead agenteket brieffel lát el és agenteket indít.
- **Projekt hozzáadása** (**+** a Projects mellett): mappaválasztóból jelölöd ki a repót (a git
  repók meg vannak jelölve); a projekt melletti **✕** törli a regisztrációt.
- **Lead**: beszélgetés a projekt lead agentjével (az első üzenet hozza létre).
- **+ Agent**: új agent indítása (feladat, szerep, Claude/Codex, modell, saját vagy megosztott
  worktree, „start after” függőségek).
- **Modellek és effort** (csúszka ikon fent): lásd lent.
- Bármely agentre kattintva látod a beszélgetést és az eszközhívásokat, írhatsz neki
  (Enter küld), leállíthatod (**Stop**) vagy törölheted (**Remove**).

A webes orchestrator egy fej nélküli agent (`orchestrator`), az `orch chat` ettől független
interaktív session; ugyanazt az állapotot látják. A felületnek nincs bejelentkezése: csak
a saját gépről érhető el, más weboldalak kéréseit elutasítja. Ne tedd ki hálózatra
(`--host 0.0.0.0`) megbízhatatlan környezetben, mert aki eléri, kódot futtató agenteket indíthat.

Az `orch chat`-ben természetes nyelven kérsz, például: *„A valami projektben készüljön el X.
Bontsd feladatokra, a megvalósítás Claude-dal menjen, a review Codexszel.”* Az orchestrator az
`orch` MCP eszközökkel dolgozik (`spawn_agent`, `ask_project_lead`, `wait_for_agents`,
`send_message`, `get_agent`, `agent_log`, `stop_agent`, `remove_agent`, …).

Ugyanez parancssorból is megy:

```sh
orch spawn -p valami -n login "Készítsd el a login végpontot ..."      # worker, saját worktree
orch spawn -p valami -r reviewer --provider codex --share login-ab12 --after login-ab12 "Review-zd a változást."
orch ask valami "Tervezd meg és valósítsd meg a ... funkciót."          # a projekt lead agentjének
orch ps                    # projekt → agent → al-agent fa, státusz, költség
orch logs login-ab12 -f    # mit csinál éppen
orch status login-ab12     # utolsó eredmény + commitok a worktree-ben
orch send login-ab12 "Javítsd a review észrevételeit: ..."              # folytatja a beszélgetést
orch wait login-ab12       # megvárja (kilépési kód: 0 kész, 1 hiba, 2 időtúllépés)
orch attach login-ab12     # kézi átvétel: interaktív claude/codex az agent sessionjében
orch stop login-ab12 / orch rm login-ab12 [--delete-branch]
```

### Modell és effort szintenként

Három szint állítható külön, providerenként (Claude / Codex): az **orchestrator**, a
**projekt lead** és a **workerek** (minden más szerep: worker, planner, reviewer, tester).
Van globális alapértelmezés, amit projektenként felül lehet írni.

```sh
orch config set orchestrator --model opus --effort high
orch config set lead --model sonnet --effort medium
orch config set worker --model haiku --effort low
orch config set worker --provider codex --model gpt-6-luna
orch config set worker --model sonnet -p valami      # csak a `valami` projektben
orch config show [-p valami]
```

Sorrend egy körnél: az agentnek kifejezetten megadott érték (`orch spawn --model/--effort`,
az űrlap mezői) → projekt beállítás → globális beállítás → a szerep `model` mezője → a CLI
saját alapértelmezése. A beállítás a kör indulásakor érvényesül, tehát a már létező agentek
következő üzenetére is hat.

### Egymásra épülő feladatok

- `after` / `--after`: az agent csak a megadott agentek (vagy run-ok) sikeres befejezése után
  indul; ha egy függőség elbukik, `skipped` lesz.
- `{{result:<agent-id>}}` a feladat szövegében: induláskor behelyettesül a másik agent
  végső válasza (pl. a planner terve a worker briefjébe).
- `base=<agent-id>`: a worktree a másik agent branchéről ágazik le.
- `share_worktree_with` / `--share`: ugyanabban a worktree-ben dolgozik (review, teszt, javítás).
- `output_schema` (MCP): JSON Schema szerinti strukturált válasz (pl. feladatlista a plannertől).
- Egy agentnek küldött üzenetek sorban futnak le; az agent megőrzi a beszélgetését
  (`claude --resume`, `codex exec resume`).

## Felépítés

```
src/orchestrator/
  models.py        Project, Agent, Run (egy kör: prompt → válasz), Event
  store.py         SQLite (WAL) – az egyetlen megosztott állapot
  git.py           worktree életciklus
  roles.py         szerepek betöltése; roles/*.md a beépített szerepek
  settings.py      modell/effort szintenként: globális + projekt felülírás
  providers/       base.py: Provider protokoll · claude.py · codex.py
  runner.py        egy run végrehajtása saját, leválasztott folyamatban
  service.py       Orchestrator homlokzat – ezt használja a CLI, az MCP szerver és a web
  mcp_server.py    az orchestrator és a lead agentek eszközei
  cli.py           `orch`
  web/             webes felület: egy oldal + JSON API a homlokzat fölött
```

**Nincs daemon.** Minden run saját `python -m orchestrator.runner <id>` folyamat: megvárja a
függőségeit, szükség esetén létrehozza a worktree-t, elindítja a CLI-t headless módban
(`claude -p --output-format stream-json`, illetve `codex exec --json`), és a kimenetet
egységes eseményekké alakítja. Az állapot a `~/.orchestrator/` alatt él (`ORCH_HOME`-mal
áthelyezhető): `orch.db`, `logs/run-<id>/` (nyers stream, stderr), `worktrees/<projekt>/<agent>`.

**Jogosultságok.** A Claude agentek alapból `--permission-mode auto`, a Codex agentek
`workspace-write` sandboxban futnak; agentenként felülírható (`orch spawn --permission ...`).

### Bővítési pontok

- **Új agent típus:** a `Provider` protokoll megvalósítása (`providers/base.py`) és
  regisztrálása a `providers/__init__.py`-ban, vagy kívülről:
  `ORCH_PROVIDERS="nev=csomag.modul:Osztaly"`.
- **Új szerep:** markdown fájl frontmatterrel (`description`, `lead`, `worktree`, `provider`,
  `model`) a `<projekt>/.orch/roles/` vagy a `~/.orchestrator/roles/` alatt; a beépítetteket
  név szerint felülírja. A `lead: true` szerep megkapja az MCP eszközöket.
- **Folyamatok (tervezés → felosztás → tesztesetek → megvalósítás → review → teszt):** az
  építőkövek megvannak (`after`, `{{result:…}}`, `base`, megosztott worktree, `output_schema`);
  egy deklaratív pipeline réteg a `service.py` fölé építhető.

## Fejlesztés

```sh
uv run pytest          # a tesztek ál-providerrel, valódi git worktree-kkel futnak
uv run ruff check . && uv run ruff format .
```
