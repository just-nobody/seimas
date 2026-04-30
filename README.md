# Seimo narių turto deklaracijos 2008–2024

Įrankis, kuris surenka Lietuvos Seimo narių turto ir pajamų deklaracijų duomenis iš VRK svetainės ir sugeneruoja interaktyvų HTML puslapį jų peržiūrai.


## Ką daro

1. Iš [vrk.lt](https://www.vrk.lt) svetainės surenka duomenis apie visus išrinktus Seimo narius per penkis rinkimų ciklus nuo 2008 iki 2024.
2. Išsaugo duomenis į SQLite duomenų bazę `seimas.db`
3. Sugeneruoja statinį interaktyvų HTML failą `index.html` su visais duomenimis

## Rezultatas

Failas `index.html` - pilnai statinis, veikia be serverio ar interneto ryšio. Jame:

- **409 unikalių Seimo narių** sąrašas su paieška ir rūšiavimu
- Kiekvieno nario turto ir pajamų deklaracijos per visas kadencijas vienoje lentelėje
- Pokyčiai tarp kadencijų
- Pajamų mokesčio dalis procentais nuo bendrų pajamų
- Paspaudus metus atidaromas originalus VRK šaltinis
- Interaktyvūs grafikai paspaudus ant vienos iš deklaracijos eilučių

### Rūšiavimo galimybės

Surūšiuojama pagal pavardes, bet galima pasirinkti ir surūšiuoti pagal bet kurį finansinį rodiklį kai surandama didžiausia finansinio rodiklio reikšmė per visas kadencijas.

## Reikalavimai ir diegimas

- Python 3.10+
- Priklausomybės:

```
pip install -r requirements.txt
```

## Duomenų surinkimas ir HTML failo generavimas

```
python seimas2008-2024.py
```

Duomenų surinkimas ir generavimas gali užtrukti iki 10 minučių.

Po paleidimo atsidaryti `index.html` naršyklėje.

## Failai

| Failas | Paskirtis |
|--------|-----------|
| `seimas2008-2024.py` | Pagrindinis skriptas: duomenų surinkimas + HTML generavimas |
| `requirements.txt` | Python priklausomybių sąrašas |
| `index.html` | Sugeneruotas interaktyvus puslapis |

## Duomenų šaltinis

[Vyriausioji rinkimų komisija (VRK)](https://www.vrk.lt) - kandidatų turto ir pajamų deklaracijos.
