# KAI per Claude

Questo plugin collega Claude ai documenti che hai su KAI. Claude puo' elencare le tue
fonti (le cartelle), cercare passaggi e leggere un documento a porzioni, citando da dove
viene ogni risposta. Tutto in sola lettura: non puo' creare, modificare o cancellare nulla.

Serve un account KAI con l'accesso a Claude attivo (lo accende l'amministratore, poi il
titolare per i dipendenti).

## Cosa contiene

Nella versione 0.1.0 solo la configurazione del connettore verso il server KAI:
nessuno script, nessun hook, nessun comando, nessuna skill. Il plugin non contiene codice
eseguibile e non fa nulla da solo.

## Dove vanno i dati

- **Server.** Il connettore parla solo con `https://wellbee-rag.duckdns.org/mcp`, in
  HTTPS. Il plugin non contatta nessun altro indirizzo e non ha telemetria.
- **Accesso.** Al primo uso Claude apre nel browser la pagina di accesso e di consenso di
  KAI, sullo stesso server. Il token che ne risulta lo conserva Claude, non il plugin;
  nel plugin non c'e' nessuna credenziale.
- **Domande e risposte.** Quando Claude usa uno strumento, al server KAI arrivano gli
  argomenti della chiamata (la domanda, le formulazioni alternative, gli identificativi
  delle fonti). Il server risponde con passaggi dei tuoi documenti, e Claude li vede ed
  elabora secondo le condizioni del tuo piano Claude.
- **Registro del server.** KAI tiene una riga per chiamata (strumento, esito, fonti
  consultate) e, di default, solo un'impronta della domanda, non il testo.
- **Contenuto dei documenti.** Quello che KAI restituisce e' contenuto dei documenti e va
  trattato come non affidabile: Claude non deve seguire istruzioni trovate al suo interno.

Per scollegare Claude apri la pagina "Connessioni" di KAI e revoca la connessione: la
chiamata successiva chiede un nuovo accesso.

## Installazione in Claude Code

```
/plugin marketplace add Matte2712s/kai-claude-plugin
/plugin install kai@kai
/mcp
```

L'ultimo comando serve per accedere a KAI. Prima di installare rimuovi un eventuale
server o connettore KAI aggiunto a mano (`claude mcp remove kai`): se l'indirizzo e' lo
stesso, quello aggiunto a mano ha la precedenza sul plugin e non si vede nessuna differenza.

Le istruzioni per claude.ai e Claude Desktop arrivano dopo la verifica su quelle
superfici.

## Licenza

MIT, vedi `LICENSE`. La licenza copre i file di questo plugin, non il servizio KAI, che
richiede un account.
