# Min Sky → iCloud

Et verktøy som flytter alle bilder og videoer fra **Telenor Min Sky** til **iCloud-bilder**,
og som er laget for at **ingenting skal kunne gå tapt** underveis, selv om det stopper midt i.

```
 Min Sky (nett)            Mac-en din                                       iCloud
┌──────────────┐  zip   ┌─────────┐  kontroll  ┌────────┐ import ┌────────┐ Apple  ┌────────┐
│ min-sky.no   │ ─────▶ │ innboks │ ─────────▶ │ arkiv  │ ─────▶ │ Bilder │ ─────▶ │ iCloud │
└──────────────┘        └─────────┘  SHA-256   └────────┘        └────────┘        └────────┘
     urørt                  urørt      permanent kopi       kontrollert mot arkivet   bekreftet
```

## Slik er det sikret at ingenting blir borte

| Risiko | Hva verktøyet gjør |
|---|---|
| Strømmen går, Mac-en sovner, du trykker Ctrl+C | Alt som er gjort, er skrevet i en database (`flytting.db`) i det øyeblikket det skjer. Kjør samme kommando igjen, så fortsetter det der det slapp. |
| En nedlasting fra Min Sky ble avbrutt eller er skadet | Hver fil i zip-filen kontrolleres med sjekksummen (CRC) som ligger i zip-filen. Ødelagte zip-filer får beskjed «last ned på nytt». Det som var i orden, beholdes. |
| Filen blir skrevet feil til disk | Hver fil skrives til en midlertidig fil og lagres skikkelig på disken. Den leses så inn igjen, og SHA-256-sjekksummen sammenlignes før den regnes som arkivert. |
| Samme bilde lastet ned flere ganger (album + «alle bilder») | Gjenkjennes på innhold (SHA-256) og lagres bare én gang. Alle steder bildet ble funnet, huskes. **Du kan trygt laste ned for mye.** |
| Stopp midt i en import til Bilder | Neste kjøring importerer gruppen på nytt med duplikatsjekk. Det som allerede kom inn, kobles til bildet som finnes, og blir ikke lagt inn to ganger. |
| Bildet fantes allerede i iCloud (f.eks. fra iPhone) | Det legges ikke inn på nytt. Er kopien i iCloud *ikke* helt identisk, importeres Min Sky-kopien i tillegg. Et duplikat er bedre enn et tap. |
| Bilder-appen tok ikke imot filen riktig | Etter import slås hvert bilde opp i Bilder-biblioteket og sammenlignes byte for byte (SHA-256) med arkivet. |
| Det er ikke lastet opp til iCloud ennå | Bilder-appen spørres om hvert enkelt bilde faktisk er lastet opp. Først da står det «FERDIG». |
| Noen sletter et bilde i Bilder etterpå | `verifiser` oppdager det. Slettede bilder importeres på nytt, og bilder i «Nylig slettet» flagges. |
| Disken blir full | Ledig plass sjekkes før hver zip-fil og hver importgruppe, og verktøyet stopper i god tid. |
| Arkivet blir skadet over tid | `kontroller-arkiv` leser alt på nytt og sjekker sjekksummene. Skadde filer repareres fra zip-filen. |
| To vinduer kjører samtidig | En lås hindrer det. |

Verktøyet **sletter aldri noe**, verken i Min Sky, i innboksen eller i Bilder.
Arkivmappen blir din fullstendige, kontrollerte sikkerhetskopi.

Til slutt får du en sjekkliste som svarer **JA** eller **NEI** på om det er trygt å si opp Min Sky.

---

## Programmet (anbefalt)

```bash
minsky-icloud app
```

Programmet åpnes i nettleseren (det kjører bare på din egen maskin, `127.0.0.1`) og tar deg gjennom fire steg:

1. **Hent fra Min Sky**: lenke til min-sky.no, knapp som åpner innboks-mappen, og en liste over nedlastingene
   som viser om hver zip-fil er lest inn, ødelagt eller fortsatt lastes ned.
2. **Velg bilder**: mappene fra Min Sky med avkrysningsbokser og miniatyrbilder. Alt er valgt i utgangspunktet,
   og du fjerner det du ikke vil ha med.
3. **Flytt til iCloud**: viser hvilken iCloud-konto Mac-en er logget inn med. Knapper for prøverunde og
   «Flytt valgte bilder», fremdrift og logg. **Stopp** stopper ved neste trygge punkt. Trykk igjen for å fortsette.
4. **Kontroll**: sjekklisten som svarer JA eller NEI på om det er trygt å si opp Min Sky.

![Skjermbilde](docs/app.png)

Kommandolinjen under gjør nøyaktig det samme, med de samme sikkerhetene.

## Hvorfor HEIC går via Bilder-appen

iCloud sin nettside (iCloud.com) og alle «direkte» iCloud-integrasjoner bruker Apples web-opplasting, og den tar
**bare imot JPEG**. HEIC-bilder (iPhone-standarden), videoer og Live Photos blir avvist. Bilder-appen på Mac tar imot
alt dette i original kvalitet, og laster det opp til iCloud selv. Derfor går programmet via Bilder-appen. Å konvertere
HEIC til JPEG for å komme rundt det ville gitt dårligere kvalitet og ødelagt Live Photos.

## Dette trenger du

* En **Mac** med macOS 12 eller nyere. Bilder-appen må være logget inn med Apple-ID-en som skal ha bildene.
* **iCloud-bilder slått på**: Bilder → Innstillinger → iCloud → «iCloud-bilder».
* **Nok iCloud-lagring** (iCloud+ 200 GB / 2 TB …). Verktøyet viser hvor mye bildene tar.
  Sjekk i Systeminnstillinger → [ditt navn] → iCloud.
* **Ledig diskplass**: omtrent 2 × størrelsen på bildene (arkiv + Bilder-bibliotek). Har du lite plass:
  * legg arbeidsmappen på en **ekstern disk** (se under), og
  * slå på «Optimaliser Mac-lagring» i Bilder → Innstillinger → iCloud.

## Steg 1: Installer (én gang)

Åpne **Terminal** (⌘+mellomrom, skriv «Terminal») og lim inn én linje om gangen:

```bash
xcode-select --install
```

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

```bash
brew install pipx && pipx ensurepath
```

Lukk Terminal og åpne den igjen. Installer deretter verktøyene:

```bash
pipx install osxphotos
```

```bash
pipx install git+https://github.com/MimirVision/MinSky-iCloud.git
```

(Er repoet privat, kan du laste det ned som zip fra GitHub, pakke det ut og kjøre
`pipx install ~/Downloads/MinSky-iCloud-main` i stedet.)

**Tillatelser:** Systeminnstillinger → Personvern og sikkerhet → **Full disktilgang** → slå på **Terminal**.
Første gang verktøyet snakker med Bilder, spør macOS om tilgang til Bilder. Svar **Tillat**.

Sjekk at alt er klart:

```bash
minsky-icloud sjekk
```

### Vil du bruke en ekstern disk?

Standardmappen er `~/MinSky-flytting`. Vil du heller bruke en ekstern disk, skriv dette i Terminal
(bytt ut `MinDisk` med navnet på disken). Da slipper du å gi `--mappe` hver gang:

```bash
echo 'export MINSKY_MAPPE="/Volumes/MinDisk/MinSky-flytting"' >> ~/.zshrc && source ~/.zshrc
```

## Steg 2: Last ned fra Min Sky

1. Gå til **min-sky.no** i nettleseren (Chrome eller Firefox anbefales) og logg inn.
2. Last ned **alle bildene og videoene**. Ifølge Telenor finnes det en funksjon for å laste ned alt fra
   Min Sky Web. Er det en grense for hvor mange du kan laste ned om gangen, tar du det i bolker, for eksempel ett år
   eller ett album av gangen. **Det gjør ingenting om noe lastes ned flere ganger.**
3. Lagre (eller flytt) zip-filene til mappen **`~/MinSky-flytting/innboks`**. Ikke pakk dem ut. Verktøyet gjør det
   selv og kontrollerer dem underveis.
   * Bruker du Safari: slå av Safari → Innstillinger → Generelt → «Åpne trygge filer etter nedlasting».
     Ellers pakker Safari ut zip-filene og sletter dem. Utpakkede mapper fungerer også, men da har vi ikke
     zip-filens sjekksummer å kontrollere mot.
4. Noter hvor mange bilder og videoer **Min Sky sier** at du har, og registrer tallet:

```bash
minsky-icloud forventet 12345
```

> **Tips:** Er det mange bilder, kan du også be Telenor/Jotta om en full kopi av dataene dine
> (retten til dataportabilitet etter GDPR). Den kan legges rett i innboksen.

Filer som fortsatt lastes ned (`.crdownload`, `.part` eller nylig endrede filer) hoppes over til neste kjøring.

## Steg 3: Prøverunde

```bash
minsky-icloud hent
```

Dette leser inn og kontrollerer alt i innboksen og legger det i `arkiv/`. Det går ingenting til iCloud ennå.

```bash
minsky-icloud kjør --antall 20
```

Dette importerer 20 bilder. Åpne Bilder og se etter albumet **«Fra Min Sky»**. Sjekk at bildene, datoene og videoene ser riktige ut.

## Steg 4: Flytt alt

```bash
minsky-icloud kjør
```

Dette kan ta timer for store samlinger. **Du kan avbryte når som helst** (Ctrl+C, lukke lokket, strømbrudd).
Kjør samme kommando igjen for å fortsette. Har du lastet ned flere zip-filer, legger du dem i innboksen og kjører
`kjør` på nytt.

## Steg 5: Vent på iCloud og kontroller

La Mac-en stå på (koblet til strøm) med **Bilder åpen** til opplastingen er ferdig. Nederst i Bilder → Bibliotek
ser du hvordan det går. Kjør dette innimellom:

```bash
minsky-icloud verifiser
```

## Steg 6: Er det trygt å si opp Min Sky?

```bash
minsky-icloud kontroller-arkiv
```

```bash
minsky-icloud status
```

Du får en sjekkliste:

```
Er det trygt å si opp / slette Min Sky?
  [✓] Bilder/videoer funnet i nedlastingene: 12345
  [✓] Alle nedlastede zip-filer er lest inn uten feil
  [✓] Antall stemmer med Min Sky: forventet 12345, funnet 12345 unike
  [✓] Alle er importert og kontrollert (0 gjenstår)
  [✓] Ingen feil, avvik eller slettede (0 trenger tilsyn)
  [✓] Alle er bekreftet lastet opp til iCloud (12345 av 12345)
  [✓] Arkivet er kontrollert

JA – alt er kontrollert.
```

Står det **NEI**, forteller verktøyet hva neste steg er. Vil du se en rapport over alle filer, kjører du denne:

```bash
minsky-icloud rapport
```

Det åpner en HTML-rapport og lager en CSV-fil du kan åpne i Numbers eller Excel.

**Anbefaling:** Ta en kopi av `arkiv`-mappen til en ekstern disk og behold den. Vent gjerne noen uker
med å si opp Min Sky, og sjekk i mellomtiden at bildene dukker opp på iPhone og iCloud.com.

---

## Alle kommandoer

| Kommando | Hva den gjør |
|---|---|
| `minsky-icloud sjekk` | Sjekker Mac, osxphotos og diskplass |
| `minsky-icloud hent` | Leser inn og kontrollerer nedlastinger og arkiverer dem (fungerer også på Windows/Linux) |
| `minsky-icloud importer [--antall N]` | Legger arkiverte bilder inn i Bilder-appen |
| `minsky-icloud verifiser [--alle]` | Kontrollerer bildene i Bilder og iCloud-opplastingen |
| `minsky-icloud kjør [--antall N]` | hent → importer → verifiser → status |
| `minsky-icloud status` | Viser fremdrift og sjekklisten |
| `minsky-icloud rapport` | Lager en HTML- og CSV-rapport over alle filer |
| `minsky-icloud forventet N` | Registrerer antallet bilder Min Sky viser |
| `minsky-icloud kontroller-arkiv` | Leser hele arkivet på nytt og sjekker sjekksummene |

Valg: `--mappe STI` (arbeidsmappe), `--album NAVN` (standard «Fra Min Sky», `--album ""` for intet album),
`--gruppe N` (filer per importrunde), `--prøv-feil-igjen`, `-v` (mer detaljer).

## Mappen

```
~/MinSky-flytting/
  innboks/       ← legg nedlastingene her (blir aldri endret)
  arkiv/         ← kontrollert kopi av hver fil, med mappestrukturen fra Min Sky
  rapporter/     ← HTML/CSV-rapporter
  logg/          ← logg over alt som skjer, og rådata fra hver import
  flytting.db    ← databasen med status for hver fil
```

Når `status` sier **JA** og du har tatt en kopi av `arkiv/`, kan du slette `innboks/` for å frigjøre plass.

## Hva betyr statusene?

| Status | Betydning |
|---|---|
| `arkivert` | Trygt kopiert og kontrollert lokalt, venter på import |
| `importerer` | Import påbegynt, fullføres ved neste kjøring |
| `importert` | Ligger i Bilder, ikke kontrollert ennå |
| `bekreftet` | Kopien i Bilder er identisk med arkivet, venter på iCloud-opplasting |
| `i_icloud` | **Ferdig**: kontrollert og lastet opp til iCloud |
| `ikke_media` | Ikke bilde eller video (ligger bare i arkivet) |
| `feil` | Bilder-appen avviste filen 3 ganger. Filen ligger i arkivet og vises i rapporten |
| `avvik` | Kopien i Bilder stemmer ikke med arkivet. Må sjekkes manuelt |
| `i_papirkurv` | Ligger i «Nylig slettet» i Bilder. Gjenopprett den der og kjør `verifiser` |

## Feilsøking

* **«Fant ikke osxphotos»**: Kjør `pipx install osxphotos` og åpne Terminal på nytt.
* **Bilder-appen spør om noe eller henger**: Svar på dialogen. Henger den, avslutter du Bilder og kjører
  `minsky-icloud kjør` igjen.
* **«feil» på noen videoer**: Noen gamle formater (`.avi`, `.wmv`, `.mkv`) støttes ikke av Bilder. De ligger i arkivet.
  Konverter dem (f.eks. med HandBrake) og legg de konverterte filene i innboksen.
* **Datoene er feil på noen bilder**: Bilder uten opptaksdato i filen (typisk fra meldingsapper) får datoen
  som ligger i zip-filen. Den kan rettes i Bilder → Bilde → Juster dato og tid.
* **Logg**: `~/MinSky-flytting/logg/minsky-icloud.log`

## Hvorfor ikke innlogging med Telenor direkte i programmet?

Min Sky (nå drevet av Jottacloud) har ingen offentlig API. En innlogging rett i programmet krever at
nettsidens interne API kartlegges, og det er planlagt som neste steg (se `docs/telenor-innlogging.md`).
Uansett må bildene innom maskinen din: verken Telenor eller Apple kan sende bilder direkte seg imellom.
Det som tar tid, er nedlasting og opplasting over nettet. Den lokale kopien koster litt diskplass, men nesten
ingen tid, og den er sikkerhetsnettet ditt hvis noe går galt.

## For utviklere

```bash
python3 -m pip install pytest && python3 -m pytest
```

Testene bruker en falsk `osxphotos` (`tests/fake_osxphotos.py`) og kjører på Linux og Mac. De simulerer blant annet
krasj under utpakking og import, avbrutte nedlastinger, CRC-feil, bilder som finnes fra før, bilder som er slettet
i Bilder, og skadet arkiv.
