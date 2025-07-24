Plotter:
Komunikace přes UDP
packet buffer - řažení vzorků a počítání chybějících packetů 
signal buffer - vždy plný, vykresluje se z něj graf - zobrazovat automaticky pouze část z x range
ukládání dat do souboru - na trigger ([čas] před až konec bufferu) nebo uložit celý buffer XX
vyřešit číslování souborů při ukládání, vždy když mám stejné jméno tak uložím o jedna větší index než mám ve složce XX

GUI
	GRAF
		dvě osy XX
		Nastavování os podle ID packetu XX
		autorange - nastaví celý signal buffer
		
	zaplnění bufferu (buffer sítě - buffer jedna) 
	ERR chybné packety
	ERR chybějící packety 
	ERR chybné vzorky
	počítadlo celkově přijatých packetů 
	reset errorových počítadel
	Možnost zdání portu pro příjem dat a odesílání CMD
	zatržítko jestli poslouchat data na všech IP nebo jen na IP odkud chodí CMD 
	možnost zadání ip a portu kam se posílají CMD
	log message 
	
	
	Tlačítka: PING, ID Packet, Register receiver (pole pro zadání adresy), Remove receiver (pole pro zadání adresy), Get receiver
	Automatizační tlačítko conect provede: get ID, register receiver s adresou plotteru a vypíše connect při úspěškosti obou. - volá pouze funkce get id a register receiver
	Tlačítka pro data: Start, start on trigger a stop 
	možnost zadat požadovaný počet packetů (0=kontinualnmí)
	set path a save buffer, pole safe before trigger [čas] (zobrazení hotovo) XX
	Ad Hoc Save tlačítko pro jednorázové uložení bufferu jinam (zobrazení hotovo) XX
	výpis path - i s jménem následujícího souboru (zobrazení hotovo) XX
	pole pro nastavení velikosti bufferu v s 
	trigger tlačítko, zatržítko safe on trigger (zobrazení hotovo) XX



pamatovat si poslední nastavení - ukládání do souboru a čtení posledního nastavení ze souboru při startu - soubor musí být čitelný pro lidi - ini standart 
vyřešit mechanismy čtení dat z více zařízení
	
buffered_socket.py
dvě vlákna
data z generátoru poslouchá kde se mu řekne, dává do bufferu, odkud si to můžu vzít, přidává info o adrese odesílatele
data z plotteru dává do druhého bufferu a přeposílá je kam se mu řekne
změna portu se zadává kdykoliv 
		
postup práce:
vyřešit optimalizaci přijmu dat, vysoké číslo ztracených packetů


ukládání
různé osy a čtení offsetu dat

	


parser.py
tahá si packety z buffered_socket.py čte je a zpracovává. zjistím co mi přišlo a podle toho se zachovám




	