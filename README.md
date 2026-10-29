# kai-claude-plugin

Marketplace e plugin Claude per [KAI](https://wellbee-rag.duckdns.org): collegano Claude
ai documenti dell'utente, in sola lettura, tramite il server MCP di KAI. Il plugin
(`plugins/kai`) contiene il connettore e, piu' avanti, le skill che insegnano a Claude a
usare bene gli strumenti. L'accesso ai documenti non dipende mai dal plugin: lo decide il
server KAI a ogni chiamata.

## Installazione (Claude Code)

```
/plugin marketplace add Matte2712s/kai-claude-plugin
/plugin install kai@kai
/mcp
```

Dettagli, dati trattati e come scollegare: [plugins/kai/README.md](plugins/kai/README.md).

## Struttura

```
.claude-plugin/marketplace.json   il marketplace "kai"
plugins/kai/                      il plugin (manifesto, .mcp.json, README, LICENSE, CHANGELOG)
contract/mcp_tools.json           copia del contratto degli strumenti del server (vedi sotto)
scripts/                          controlli usati dalla CI
tests/                            test dei controlli
.github/workflows/ci.yml          la CI
```

## Contratto con il server

`contract/mcp_tools.json` e' una copia dell'istantanea che il repository del server KAI
genera dalle risposte reali di `initialize` e `tools/list`: istruzioni, nomi, descrizioni
e schemi degli argomenti degli strumenti. La CI (`scripts/check_contract.py`) fallisce se
una skill cita uno strumento o un parametro che li' non esiste. Quando il server cambia
contratto si aggiorna questa copia a mano, nello stesso commit delle skill interessate.

## Convenzioni per le skill

`scripts/check_contract.py` legge le skill (`SKILL.md`, le `references/*.md` accanto e i
`commands/*.md`) e controlla solo cio' che e' scritto cosi':

- Uno strumento si cita col nome completo `mcp__plugin_kai_kai__<strumento>`, che deve
  esistere nel contratto.
- Una chiamata si scrive in un frammento di codice come `` `cerca(domanda, fonti=...)` ``:
  ogni parametro comincia col suo nome (prima di `=` o `:`), che deve essere nello schema
  dello strumento; `...` da solo e' ammesso. Un valore senza nome o `[opzionale]` non vale.
- Un parametro si cita come `` `cerca.fonti` ``, da solo nel frammento di codice.
- Il frontmatter di `SKILL.md` ha solo `name` (uguale al nome della cartella) e
  `description` (non vuota, al massimo 1536 caratteri), su una riga o a blocco (`>`, `|`);
  un valore con `: ` va tra virgolette.

Il dettaglio e' nel commento in testa allo script.

## Sviluppo

```
python -m unittest discover -s tests
python scripts/check_contract.py
python scripts/check_tree.py
claude plugin validate --strict plugins/kai
claude plugin validate --strict .
```

Versioni: il numero e' in `plugins/kai/.claude-plugin/plugin.json` (gli utenti ricevono un
aggiornamento solo se cambia) e ogni rilascio ha una voce in `plugins/kai/CHANGELOG.md`.

Per la pubblicazione nella directory dei plugin mancano ancora un'icona e l'indirizzo
dell'informativa privacy (`claude plugin validate --strict --profile directory` li segnala).

## Licenza

MIT, vedi [LICENSE](LICENSE).
