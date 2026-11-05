# Plan: logg inn med Telenor direkte i programmet

**Mål:** I steg 1 logger du inn med Telenor-brukeren din og velger album. Programmet henter bildene selv,
uten manuelle zip-nedlastinger.

## Hvorfor det ikke finnes ennå

Min Sky (driftet av Jottacloud siden 2025) har ingen offentlig dokumentert API. Programmet må derfor snakke med
de samme interne adressene som nettsiden min-sky.no bruker. De har vi ikke kartlagt ennå: utviklingsmiljøet
der programmet ble laget, har ikke tilgang til min-sky.no.

## Slik kan du hjelpe (5 minutter, i Chrome)

1. Åpne min-sky.no og logg inn.
2. Trykk ⌥⌘I (Mac) eller F12 (Windows) og velg fanen **Network**. Huk av for **Preserve log**.
3. Last siden på nytt (⌘R), åpne et album, bla litt, og last ned **ett** bilde.
4. Høyreklikk i listen → **Save all as HAR (sanitized)**. «Sanitized» betyr at Chrome fjerner innloggingsnøkler
   og informasjonskapsler fra filen.
5. Send filen til utvikleren. Den inneholder adressene og formatet programmet trenger, men ikke passordet ditt.

## Slik blir det bygget

* Innloggingen skjer i Telenors egen innloggingsside (Telenor ID/BankID). Programmet ser aldri passordet ditt,
  bare en nøkkel som gir tilgang til Min Sky.
* Albumlisten og bildene hentes rett inn i «Velg bilder»-steget.
* Hvert bilde lastes ned, kontrolleres (størrelse og eventuell sjekksum fra Min Sky) og registreres i databasen
  på samme måte som i dag. Resten av kjeden (Bilder-appen → iCloud → kontroll) er uendret.
* Programmet sammenligner antallet i Min Sky med det som er hentet, per album. Da blir «Antall stemmer»-sjekken automatisk.
