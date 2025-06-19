"""
spustím program 
Načítání dat přes UDP 
zobrazím GUI
    graf se dvěma Y
    zaplnění bufferu
    zadání adresy zdroje dat s žádostí o data
    errory
    info
po zadání adresy a požádání si řeknu o identifikační packet, uložím si z něj data do proměných
    Identifikační packet:
        zjistím počet kanálů a pořadí dle typu dat
        zadám přepočet pro každý kanál
        nastavím osy
        zobrazím informace o hardware a firmware
        informace o úspěšné identifikaci (conectit/disconectit)

zažádám o data tlačítkem (změna tlačítka na ukončovací sběru dat)

datový packet: 
seřazení packetů v bufferu podle pořadového čísla a vykreslení vždy za 1/30 s, kontrola chybějících packetů
provést CRC a případně označit packet jako chybný
zjistit počet špatných vzorů z hlavičky 


jeden packet s více typy dat (200 od jednoho typu, pak 200 od dalšího)
ukládání dat do bufferu
vykreslování dat z bufferu do grafu podle přepočtu a typu dat
dvě osy Y (U a I)
mazání dat z bufferu po vykreslení
vykreslování naplněnosti bufferu - procenta a počet uložrených vzorků
zobrazit zpoždění - ulažená data ku rychlosti příchodu dat

errory: 
    počet stracených packetů
    počet špatně přečtených dat
    počet špatně přenesených packetů (CRC) vůči správným (jak absolutně tak v procentu)

    

sinus_generator.py
spustím program, řeknu port na kterém má poslouchat - parametr při spuštění
poslouchám
přijde žádost o identifikaci
identifikuju se - přepočet z binárky na čísla (32 767 = 200 A), identifikátor hardware a firmware, počet kanálů
    buď U+I nebo U+U+I - řekni jak jsou data po sobě (počítám od jedničky)
    označení že jde o identifikační packet (0)

poslouchám, přijde žádost (UDP packet) o data 
začínám vysílat data

generuje signál sinusoidy a posílá protokolem UDP jako binárku v rozsahu int 16 (=32 767)
possílání dat na dotaz z plotteru
identifikařní packet s udaji o signálu zaslaný na požádání
typ generovaného signálu a podoba packetu: 
dva nebo 3 signály - sin (+sin posunutý) a triangel 
zadám počet kanálů a rozsahy (maximální hodnoty)
jeden packet obsahuje 200 hodnoto od každého signálu, prvně jeden signál, pak druhý
začátek paketu:
    - označení že jde o datový packet (1) číslo o velikosti 16b
    - pořadové číslo - pakety nemají čas, ale jsou očíslované, počítám od 0 (16b číslo)
    - informační číslo kolik vzorků se nepovedlo přečíst (bude tam 0 nebo zapínatelný generátor náhodných čísel) 8b
        pro každý kanál zvlášť
    - CRC - kontrola kopletnosti přenosu dat (jakoby kontrolní součet) 32b 
        celkový počet B %4 = 0, volné B před CRC
na paremetru můžeš nastavít - náhodně zahoď občas nějaký packet

"""