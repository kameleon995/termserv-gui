# E-mail powitalny dla administratorów IT

Szablon wiadomości dla administratorów, którzy korzystają ze wspólnego konta na
serwerze konsol. Skopiuj tekst poniżej, podmień pola i wyślij. Jeśli polityka
bezpieczeństwa tego wymaga, hasło wyślij osobnym kanałem.

| Pole | Wstaw |
| --- | --- |
| `{{USER}}` | nazwa wspólnego konta na serwerze termserv |
| `{{IP}}` | adres IP lub nazwa hosta serwera termserv |
| `{{PASSWORD}}` | hasło wspólnego konta |
| `{{PORT_LIST}}` | który port to które urządzenie (opcjonalnie; usuń sekcję, jeśli niepotrzebna) |

---

**Temat:** Dostęp do serwera konsol szeregowych

Cześć,

mamy serwer konsol szeregowych: konsole routerów, switchy i serwerów są dostępne
przez SSH z dowolnego komputera (Linux, macOS i Windows 10/11 mają klienta SSH
wbudowanego, działa też PuTTY).

**Dane logowania (konto wspólne)**

- Serwer: `{{IP}}`
- Użytkownik: `{{USER}}`
- Hasło: `{{PASSWORD}}`

To konto wspólne dla całego zespołu, więc nie przekazujcie hasła poza zespół.
Konto nie daje powłoki na serwerze, tylko dostęp do konsol.

**Jak się połączyć**

Każdy port konsoli ma własny port SSH: port 1 to 2001, port 2 to 2002 itd.
(2000 + numer portu). W terminalu:

    ssh -p 2001 {{USER}}@{{IP}}

Podaj hasło i naciśnij Enter raz lub dwa razy, żeby konsola się odezwała.

Zawsze wpisuj `{{USER}}@` przed adresem. Bez tego ssh wyśle nazwę Twojego lokalnego
użytkownika i logowanie się nie uda, nawet z dobrym hasłem. W PuTTY: typ połączenia
SSH, Host `{{IP}}`, Port 2001 (itd.), nie "Serial": prędkość i parzystość są już
ustawione po stronie serwera.

**Porty**

{{PORT_LIST}}

**Skróty klawiszowe** (Ctrl-E, potem c, potem klawisz)

- `Ctrl-E c .` rozłączenie
- `Ctrl-E c f` przejęcie pisania, gdy ktoś inny jest na porcie
- `Ctrl-E c l 0` wysłanie sygnału break
- `Ctrl-E c ?` pomoc

**Praca na wspólnym porcie**

Na jednym porcie może być kilka osób naraz. Pisać może tylko pierwsza osoba, która
się połączyła; pozostali widzą to samo na żywo w trybie tylko do odczytu, dopóki nie
przejmą pisania przez `Ctrl-E c f`. Ponieważ wszyscy logujemy się tym samym kontem,
serwer nie pokaże, kto konkretnie siedzi na porcie, więc zanim przejmiesz konsolę,
daj znać na kanale zespołu.

Przed rozłączeniem wyloguj się z urządzenia (`exit`, `logout`, `end`), żeby następna
osoba nie trafiła do Twojej uprzywilejowanej sesji.

Wszystko, co dzieje się na konsolach, jest logowane, łącznie z tym, co wpisujesz
(także hasła do urządzeń).

---
