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
  (Enter küld), leállíthatod (**Stop**) vagy archiválhatod (**Archive**; lásd lent). A
  **Queued** lista üzeneteit indulás előtt átírhatod (ceruza) vagy visszavonhatod (**✕**).
- **Modell és effort az agent fejlécében** (minden agentnél, az orchestratornál és a leadeknél
  is): a lenyílóban a provider modelljei verzióval (Claude-nál azok, amelyeket a telepített Claude
  Code `/model` választója kínál: a CLI `initialize` válaszából, modellkérés nélkül, folyamatonként
  egyszer lekérve; ha ez nem sikerül, az aliasok és mellettük `model list unavailable: showing
  aliases`), utána a legutóbbi kör ténylegesen használt modellje (`→ claude-fable-5-2`), `other…` alatt tetszőleges név (Enterrel érvényes, Esc
  vagy a mező elhagyása elveti), és az `inherit (…)` sor, amely a saját értéket törli
  (zárójelben, ami akkor érvényes: a szint-beállítás vagy `CLI default`). A saját érték kiemelt és `own` jelölést kap. A változás a
  következő körtől érvényes, a futó kört nem érinti. Meglévő sessionnél a modellváltás új prompt
  cache-t kezd (Claude-nál és Codexnél mérve): a következő kör a teljes beszélgetést teljes áron
  olvassa újra. Az effort váltása Codexnél megtartja a cache-t, Claude-nál újat kezdhet (nem
  mértük). A Codex folytatott sessionben is modellt vált (`codex exec resume -m`; a saját
  figyelmeztetését a naplóba írja).
- Az **idle · 3 agents working** állapot azt jelenti, hogy az agent nem dolgozik, csak az általa
  indított agentek: most is írhatsz neki, és értesítést kap, amikor azok végeznek. A várakozó
  üzenetek a **Queued** listában látszanak, az értesítések (`notification`) halvány rendszer-
  üzenetként jelennek meg a beszélgetésben.

A webes orchestrator egy fej nélküli agent (`orchestrator`), az `orch chat` ettől független
interaktív session; ugyanazt az állapotot látják. A felületnek nincs bejelentkezése: csak
a saját gépről érhető el, más weboldalak kéréseit elutasítja. Ne tedd ki hálózatra
(`--host 0.0.0.0`) megbízhatatlan környezetben, mert aki eléri, kódot futtató agenteket indíthat.

Az `orch chat`-ben természetes nyelven kérsz, például: *„A valami projektben készüljön el X.
Bontsd feladatokra, a megvalósítás Claude-dal menjen, a review Codexszel.”* Az orchestrator az
`orch` MCP eszközökkel dolgozik (`spawn_agent`, `ask_project_lead`, `wait_for_agents`,
`send_message`, `withdraw_message`, `get_agent`, `agent_log`, `stop_agent`, `archive_agent`, …).
A `withdraw_message` egy még el nem indult, általad küldött üzenetet von vissza, vagy
`replacement`-tel átírja a szövegét.

Ugyanez parancssorból is megy:

```sh
orch spawn -p valami -n login "Készítsd el a login végpontot ..."      # worker, saját worktree
orch spawn -p valami -r reviewer --provider codex --share login-ab12 --after login-ab12 "Review-zd a változást."
orch ask valami "Tervezd meg és valósítsd meg a ... funkciót."          # a projekt lead agentjének
orch ps                    # projekt → agent → al-agent fa, státusz, modell/effort, tokenek, költség
orch logs login-ab12 -f    # mit csinál éppen
orch status login-ab12     # utolsó eredmény, dolgozó al-agentek, várakozó üzenetek, commitok
orch send login-ab12 "Javítsd a review észrevételeit: ..."              # folytatja a beszélgetést
orch wait login-ab12       # megvárja (kilépési kód: 0 kész, 1 hiba, 2 időtúllépés)
orch attach login-ab12     # kézi átvétel: interaktív claude/codex az agent sessionjében
orch withdraw 42           # a 42-es, még el nem indult üzenet visszavonása (#42 az `orch status` listájában)
orch edit 42 "Új szöveg"   # ugyanennek a szövegét cseréli indulás előtt
orch stop login-ab12       # leállítja a futó és várakozó köreit
orch archive login-ab12    # archiválás (lásd lent); orch restore login-ab12 visszahozza
```

Az `orch ps` státuszoszlopában az `idle·3↓` azt jelenti, hogy az agent maga tétlen, de 3
al-agentje még dolgozik (a `running·3↓` pedig, hogy közben a saját turnje is fut); a `+2` két
további sorban álló turnt jelez.

### Archiválás

A kész agentek archiválhatók: eltűnnek az `orch ps`, a webes lista és a `list_agents`
alapnézetéből, de minden megmarad (rekord, runok, napló, branch, worktree). Az archívum:
`orch ps --archived`, a webes **Archive** nézet (a bal oldali lista alján), illetve
`list_agents(archived=true)`. Visszahozni `orch restore <id>`-vel, a felületen a **Restore**
gombbal vagy `archive_agent(restore=true)`-val lehet; egy archivált agentnek küldött üzenet
(CLI, web, `send_message`, `ask_project_lead`) is visszahozza.

```sh
orch archive login-ab12          # nem mergelt commit vagy commitolatlan változás esetén rákérdez (--yes: nem)
orch archive --safe -p valami    # minden tétlen agent, amelynek a munkája biztonságban van
orch ps --archived [-p valami]
orch restore login-ab12
orch rm login-ab12 [--delete-branch]   # végleges törlés: csak archivált agentet, worktree-vel együtt
orch config archive-after 48 [-p valami]   # automatikus archiválás ennyi tétlen óra után; 0 vagy off: soha
```

Dolgozó agent (várakozó vagy futó köre van, vagy valamelyik al-agentjének) nem archiválható.
A munka akkor **biztonságos**, ha az agentnek nincs saját worktree-je (planner, reviewer, megosztott
worktree), vagy a branche teljesen be van mergelve a projekt alap-branchébe és a worktree-ben
nincs commitolatlan változás (vagy a worktree már nem létezik). Az **automatikus archiválás**
(alapból 24 tétlen óra után; globálisan és projektenként állítható, a **Modellek és effort**
ablakban is) csak tétlen, biztonságos munkájú agentet archivál, lead-et és az orchestratort
soha; git hiba esetén sem. Nincs hozzá daemon: a listázások (webes lekérdezés, `orch ps`,
`list_agents`) futtatják, folyamatokon át legfeljebb 5 percenként. Végleges törlés csak az
archívumból és csak a felhasználó kezéből lehetséges; az agentek az MCP-n csak archiválnak.

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

Egy meglévő agent saját értéke (a felület fejlécében vagy parancssorból) a következő körtől él:

```sh
orch model login-ab12                       # érvényes, saját és örökölt érték
orch model login-ab12 --model opus --effort high
orch model login-ab12 --model ""            # törli: újra a szint-beállítás érvényes
orch model orchestrator --effort high       # az orchestratort is (első használatkor létrejön)
```

A modell bármilyen név lehet, de egyetlen szó: szóköz, vezérlőkarakter, `-` kezdet és 200
karakternél hosszabb név nem adható meg (az `orch spawn`, az `orch config set` és az MCP
`spawn_agent` is így ellenőriz; a korábban eltárolt értékek továbbra is olvashatók). Webes API: `POST /api/agents/<id>/model`
`{"model": "...", "effort": null}` (a `null` töröl, a hiányzó kulcs nem változik).

Az `orch chat` az **orchestrator** szint globális Claude beállításával indul; `--model` és
`--effort` felülírja.

### Token- és költségmérés

Minden run eltárolja a saját tokenjeit (input, output, cache olvasás, cache írás), a költségét
és a CLI által ténylegesen használt modellt (`run_usage` tábla). A Codex nem ad költséget.
Folytatott sessionnél a CLI-k részben a teljes sessionre összesített számot adnak (Claude
`total_cost_usd`/`modelUsage`, Codex `turn.completed`); ilyenkor az előző runok összegét
levonva runonkénti érték kerül tárolásra. Az `orch ps`/`orch status`, a `list_agents` és
`get_agent` MCP eszközök (utóbbi az utolsó 10 run adataival) és a webes felület mutatják.
A `model`/`effort` mező a következő körre érvényes értéket mutatja; az agentnek kifejezetten
megadott érték a részletes nézetben (`get_agent`, `orch status`) a `model_override`/
`effort_override` mezőben látszik. A listák (`list_agents`, `wait_for_agents`) agentenként
csak a `cost_usd` összeget és egy rövid `tokens` objektumot (`in`, `out`, `cache_read`,
`cache_write`) adnak.

### Mit tölt be egy agent (loadout)

Loadout nélkül minden agent a felhasználó teljes globális Claude Code / Codex konfigurációját
örökölné (claude.ai connectorok, pluginek, skillek, MCP szerverek): ez kérésenként kb. 25–31k
input token volt, a szerep és a feladat pedig ebből csak kb. 1,6k. Ezért minden szerep
deklarálja, mit tölt be:

| kulcs | jelentés | CLI |
|---|---|---|
| `tools` | beépített eszközök vesszővel; `default` = a CLI alapkészlete, `none` = egy sem (MCP eszközökre nem hat) | `--tools` |
| `mcp` | `strict` = csak az orchestrator saját szervere (lead szerepeknél); `inherit` = user, projekt, plugin és claude.ai connector szerverek is | `--strict-mcp-config` |
| `setting_sources` | `project,local` = a user szint (user pluginek, skillek, parancsok, hookok) nélkül; a projekt `CLAUDE.md`-je és `.claude/` mappája betöltődik; `user,project,local` = minden | `--setting-sources` |
| `slash_commands` | `false` = semmilyen skill és slash parancs (a projektéi sem) | `--disable-slash-commands` |
| `codex_user_config` | `false` = a Codex nem olvassa a `$CODEX_HOME/config.toml`-t (a login megmarad) | `--ignore-user-config` |
| `memory` | `true` = `Write` és `Edit` is, de csak a session saját memóriamappájába (`~/.claude/projects/<projekt>/memory/`); minden más célt egy hook elutasít. Hatástalan, ha a `tools` már ad író eszközt (`default` is), az `orch chat`-ben és Codexnél | `--settings` (PreToolUse hook) |

Beépített alapértékek: minden szerep `mcp = strict`, `setting_sources = project,local`,
`slash_commands = false`, `codex_user_config = false`; az eszközök:

| szerep | `tools` |
|---|---|
| orchestrator, lead | `Bash,Read,Glob,Grep` (+ az `orch` MCP eszközök), `memory = true` |
| worker, tester | `Bash,Read,Edit,Write,Glob,Grep` |
| reviewer | `Bash,Read,Glob,Grep` |
| planner | `Bash,Read,Glob,Grep,WebSearch,WebFetch` |

A `memory` a Claude Code memóriáját (egy fájl tényenként és a `MEMORY.md` index) tartja
életben az orchestratornál és a leadeknél: a lead memóriája a projekt sajátja, közös a
tulajdonos ott futó interaktív sessionjeivel. A korlát csak a `Write`/`Edit` eszközökre
vonatkozik, a `Bash`-re nem. Csak akkor lép életbe, ha a session hookjai bizonyítottan futnak:
a runner a promptot csak a hook által létrehozott jelzőfájl után küldi el; ha a hookok ki
vannak kapcsolva (`disableAllHooks`, policy `allowManagedHooksOnly`), a kör `Write`/`Edit`
nélkül indul újra. Ezt az agent megjegyzi (`~/.orchestrator/hooks-off/<agent>.json`): a
következő körök várakozás nélkül így indulnak, új sessionben vagy egy nap után újra
próbálja; az állapotváltást egy `info` esemény jelzi. Ha a CLI a jelzőfájl előtt kilép (pl.
lejárt login), a kör a saját hibájával bukik el, újraindítás nélkül. Az `orch chat` ezért nem kapja meg. Ha a
memóriamappa helye nem állapítható meg biztosan (szimlinkelt `.git`, a mappát áthelyező
környezeti változók), minden írást elutasít; `autoMemoryDirectory` mellett csak az
alapértelmezett mappa írható, az áthelyezett nem.

A beépített szerepeknek nincs `Skill` eszközük, ezért a skillek és slash parancsok ki vannak
kapcsolva. Projekt skillek/parancsok használatához az adott szerepnél `slash_commands = true`
és a `tools` közé a `Skill` kell. (A `scripts/loadout-after.json` mérés még
`slash_commands = true` mellett készült; a különbség kb. 30 token.)

Saját szerepfájl, amely egyik kulcsot sem adja meg: `mcp = strict`,
`setting_sources = project,local`, `tools = default` (a `Skill`-lel együtt),
`slash_commands = true`, `codex_user_config = false`. Üres érték (`tools:`) hiba.

Felülírás szerepfájl másolása nélkül: `<projekt>/.orch/loadout.toml` és
`~/.orchestrator/loadout.toml`, `[default]` (minden szerep) és `[<szerep>]` táblákkal, ugyanezekkel a
kulcsokkal. Kulcsonként az első találat nyer: projekt `[szerep]` → projekt `[default]` →
home `[szerep]` → home `[default]` → a szerep frontmattere → a fenti alapérték. Hibás fájl
vagy érték esetén a run a fájl nevét és a hibát megadva elbukik; ismeretlen szerep nevű
táblát (pl. elírt `[wroker]`) figyelmen kívül hagy, és `info` eseményként jelez.

**Ki érintett, és mi a teendő.** A `user` forrás elhagyása a teljes
`~/.claude/settings.json`-t kihagyja, nem csak a plugineket: az `env` blokkot (pl.
Bedrock/Vertex/proxy útválasztás), az `apiKeyHelper`-t, a `model`-t, a jogosultságokat és a
hookokat is. Aki így hitelesít, annak az agentjei a frissítés után nem jutnak el a modellig;
akinek ott van a `model` és nincs orch szint-beállítása, csendben a CLI alapmodelljét kapja.
Codexnél a `--ignore-user-config` a `config.toml` `model`, `model_provider` és profil
beállításait is elhagyja (a login megmarad). Ilyenkor a `~/.orchestrator/loadout.toml`-ba:

```toml
[default]
setting_sources = "user,project,local"   # Claude: a ~/.claude/settings.json újra él
codex_user_config = true                 # Codex: a ~/.codex/config.toml újra él
```

(A modell az `orch config set` szint-beállításával is megadható.) Egy projekt, amely a user
szintű pluginekre, skillekre és plugin MCP szerverekre támaszkodik, mindhárom réteget
visszakapcsolja. A `tools` szerepenként adott, ezért a `Skill`-t minden szerephez külön kell
hozzáadni, amelyiknek kell:

```toml
# <projekt>/.orch/loadout.toml
[default]
setting_sources = "user,project,local"   # user pluginek, skillek, parancsok
mcp = "inherit"                          # plugin, user és connector MCP szerverek
slash_commands = true

[worker]
tools = "Bash,Read,Edit,Write,Glob,Grep,Skill"

[tester]
tools = "Bash,Read,Edit,Write,Glob,Grep,Skill"

[lead]
tools = "Bash,Read,Glob,Grep,Skill"
```

A zászlókat minden kör megkapja, a `--resume`/`exec resume` körök is. Az `orch chat` az
`orchestrator` szerep loadoutját használja (csak a home szintű `loadout.toml` hat rá), és a
`Bash` mindig benne marad az `orch` parancsokhoz; a slash parancsokat viszont sosem kapcsolja
ki (a `--disable-slash-commands` az interaktív `/compact`, `/clear`, `/model`, `/context`
parancsokat is elvenné), csak a headless agenteknél hat a `slash_commands`. A Codex a `tools`/`mcp`/`setting_sources`/
`slash_commands` kulcsokat nem ismeri; `codex_user_config = false` mellett a user
konfiguráció modellje, MCP szerverei és pluginjei sem töltődnek be (a modell a Codex saját
alapértéke, ha nincs beállítva), a lead szerepek `orch` eszközei pedig jóváhagyás nélkül
futnak.

**Figyelem:** a változás előtt indított session a régi listákat (eszközök, skillek, MCP
utasítások) a beszélgetés előzményében hordozza, így az ott folytatott körök továbbra is
nagyok; a megtakarítás teljesen csak az új agenteknél jelentkezik.

### Egymásra épülő feladatok

- `after` / `--after`: az agent csak a megadott agentek (vagy run-ok) sikeres befejezése után
  indul; ha egy függőség elbukik, `skipped` lesz.
- `{{result:<agent-id>}}` a feladat szövegében: induláskor behelyettesül a másik agent
  végső válasza (pl. a planner terve a worker briefjébe). A strukturált válasz egészben, tömör
  JSON-ként kerül be (vágott JSON-nak nincs haszna); a szöveges válasz 4000 karakternél
  elvágódik, utána egy sor a teljes hosszal és az eredményfájl útvonalával.
  `{{result:<agent-id>:full}}` a teljes szöveget adja.
- `base=<agent-id>`: a worktree a másik agent branchéről ágazik le.
- `share_worktree_with` / `--share`: ugyanabban a worktree-ben dolgozik (review, teszt, javítás).
- `output_schema` (MCP): JSON Schema szerinti strukturált válasz (pl. feladatlista a plannertől).
  Ha a hívó nem ad meg sémát, az MCP `spawn_agent` és `send_message` a szerep alapsémáját
  használja (worker, reviewer, tester, planner: tömör, rögzített mezők; lead és orchestrator:
  szabad szöveg); `freeform: true` prózai választ kér, a saját `output_schema` mindig nyer. A
  CLI (`orch spawn/send/ask`) és a web API szabad szöveget kap.
- Egy agentnek küldött üzenetek sorban futnak le; az agent megőrzi a beszélgetését
  (`claude --resume`, `codex exec resume`).

### Értesítések

Az orchestrator és a leadek nem várakoznak a gyerekeikre: kiosztják a munkát, a kört rövid
státusszal lezárják, így közben üzenetet kaphatnak (a felhasználó üzenete nem áll be egy
várakozó kör mögé). Amikor egy gyerek agent és minden al-agentje tétlen, a szülő egy automatikus
`[orch notification]` kört kap: rövid fejléc (rendszeresemény, nem a felhasználó vagy a szülő
üzenete, semmit nem hagy jóvá), utána tömör JSON agentenként az utolsó befejezett run
válaszával (agentenként 4000, összesen 10000 karakterig; a többi `get_agent`-tel olvasható) és
a még futó gyerekek listájával.

- Csak a szülő által küldött (MCP `spawn_agent`, `send_message`, `ask_project_lead`) runok és a
  gyerek azon értesítési körei kerülnek bele, amelyek a szülőnek végzett munkáról szólnak (amit
  a gyerek a szülő megbízása közben indított); a CLI-ből, webről kért munka és a régi runok nem.
- `notify: "each"` (alapértelmezés): szól, amint ez az agent kész; `notify: "all"`: csak ha
  minden agent kész, amelynek a szülő munkát küldött. A válasz első mezője, a `next` megmondja,
  felébresztik-e a hívót (az interaktív `orch chat` sessiont soha: ott `wait_for_agents`).
- Az értesítés szövege az induláskor készül, egy rövid várakozás után
  (`ORCH_NOTIFY_DELAY`, alapból 5 mp), így az egyszerre érkező eredmények egy körbe kerülnek; ha
  addigra nincs mit jelenteni, vagy el sem indul (leállítás, elhalt runner), a run nyom nélkül
  törlődik. Ha az értesítési kör elbukik, az eredményei újra kézbesíthetők: a következő
  értesítéssel jönnek (automatikus újrapróbálás nincs), addig `wait_for_agents` adja őket.
- Értesítést csak egy run vége vált ki, várakozás (`orch wait`, `wait_for_agents`) soha; egy
  leállított agent így csendben marad a következő valódi befejezésig.
- Minden eredmény egyszer jut el a szülőhöz: amit `wait_for_agents` már visszaadott, nem jön
  értesítésben, és az értesítésben kapott eredményt egy későbbi `wait_for_agents`
  `already_delivered`-ként jelzi. A `wait_for_agents` (alapból 120 mp) az al-agentekre is vár,
  és azonnal visszatér, ha a hívónak üzenete érkezett.
- Egy agentnek egyszerre egy köre fut; az üzenetek sorrendje marad, a várakozó értesítés a
  futásra kész üzenetek után következik. A `get_agent` részletes nézete `queue` mezőben mutatja
  a várakozó runokat, minden nézet `children_running` mezőben a futó leszármazottak számát.

## Felépítés

```
src/orchestrator/
  models.py        Project, Agent, Run (egy kör: prompt → válasz), Event
  store.py         SQLite (WAL) – az egyetlen megosztott állapot
  git.py           worktree életciklus
  roles.py         szerepek betöltése; roles/*.md a beépített szerepek
  settings.py      modell/effort szintenként: globális + projekt felülírás
  loadout.py       mit tölt be egy agent (eszközök, MCP, setting források); loadout.toml
  memory_guard.py  PreToolUse hook: a `memory` kulcs Write/Edit-je csak a memóriamappába ír
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
áthelyezhető): `orch.db`, `logs/run-<id>/` (nyers stream, stderr, `result.md` a teljes végső válasszal –
elbukott runnál az utolsó szöveg vagy a hiba –, strukturált válasznál `result.json`),
`worktrees/<projekt>/<agent>`. A `result.md` útvonala a részletes nézetben (`get_agent`)
`result_file`; a listákban nem szerepel. Leállított run és összeomlott runner nem ír ilyet.

**Jogosultságok.** A Claude agentek alapból `--permission-mode auto`, a Codex agentek
`workspace-write` sandboxban futnak; agentenként felülírható (`orch spawn --permission ...`).

### Bővítési pontok

- **Új agent típus:** a `Provider` protokoll megvalósítása (`providers/base.py`) és
  regisztrálása a `providers/__init__.py`-ban, vagy kívülről:
  `ORCH_PROVIDERS="nev=csomag.modul:Osztaly"`.
- **Új szerep:** markdown fájl frontmatterrel (`description`, `lead`, `worktree`, `provider`,
  `model`, `output_schema` és a loadout kulcsok) a `<projekt>/.orch/roles/` vagy a `~/.orchestrator/roles/` alatt; a beépítetteket
  név szerint felülírja. A `lead: true` szerep megkapja az MCP eszközöket. Az
  `output_schema: <fájl>` a szerep alapsémája (JSON Schema fájlnév, nem útvonal); a fájlt a
  szerepekkel azonos sorrendben keresi (`<projekt>/.orch/roles/`, `~/.orchestrator/roles/`,
  beépített), így egy projekt a beépített sémát is felülírhatja. `none` vagy hiánya: szabad
  szöveg. A séma a Claude `--json-schema` és a Codex `--output-schema` számára is legyen
  szigorú: minden mező a `required`-ben, `additionalProperties: false`, az opcionális érték
  nullable vagy üres lista.
- **Folyamatok (tervezés → felosztás → tesztesetek → megvalósítás → review → teszt):** az
  építőkövek megvannak (`after`, `{{result:…}}`, `base`, megosztott worktree, `output_schema`);
  egy deklaratív pipeline réteg a `service.py` fölé építhető.

## Fejlesztés

```sh
uv run pytest          # a tesztek ál-providerrel, valódi git worktree-kkel futnak
uv run ruff check . && uv run ruff format .
```
