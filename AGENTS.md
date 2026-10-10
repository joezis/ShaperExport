# ShaperExport – FreeCAD Draft-Workbench-Plugin

## Auftrag und Anforderungen

- Pluginname: **ShaperExport**.
- Die exportierte SVG ist für die Fräsmaschine **Shaper Origin** optimiert. Diese
  interpretiert die Farben von Linien und Flächenfüllungen als Codierung für die Art
  der Fräsungen; Exportfarben sind daher Teil der Fertigungsanweisung.
- In der **Draft-Workbench** ausgewählte Objekte sollen als Shaper exportierbar sein.
- Endpunkte, deren Koordinaten exakt übereinstimmen, sollen zu einem Pfad
  verbunden werden. Endet ein Pfad wieder an seinem Startpunkt, wird er im SVG
  geschlossen.
- Nach dem Export soll eine Statistik ausgegeben werden, wie viele
  geometrische Objektpaare (zum Beispiel Linien, Kreise und Polygone) sich
  überlappen.
- Der Befehl soll im Draft-Menü erscheinen und von dort aufrufbar sein.

## Dateien und Verwendung

- `InitGui.py` registriert den GUI-Befehl `ShaperExport_Export` und versucht, ihn
  über den Draft-Menümanipulator als **Für Shaper exportieren ...** anzubieten.
- `shaper_export.py` liest Kanten der ausgewählten Shape-Objekte, erzeugt
  zusammenhängende SVG-Pfade, öffnet zuerst den Dialog **Taschengröße**, der maximale Taschenbreite und -höhe
  in Millimetern abfragt, und danach den Speichern-Dialog. Der Taschengrenzwert gilt für
  beide Maße, wird für den nächsten Export gespeichert und kann im Dialog
  abgebrochen werden. Die Statistik erscheint in der FreeCAD-Konsole sowie,
  sofern verfügbar, in einem Dialog.
- `__init__.py` kennzeichnet den Python-Modulordner.
- `.gitignore` schließt Python-Bytecode (`__pycache__`, `*.pyc`, `*.pyo`, `*.pyd`) aus dem Repository aus.
- Installation: den Ordner `ShaperExport` im FreeCAD-Mod-Verzeichnis belassen,
  FreeCAD starten, in Draft Objekte auswählen und den Menüeintrag aufrufen.

## Geometrieverarbeitung und Einschränkungen

- Die Geometrie wird auf die XY-Ebene projiziert; Z wird verworfen. Die
  Implementierung versucht, die globale Objektplatzierung auf die Shape-Kopie
  anzuwenden.
- Der Export nutzt FreeCADs `importSVG.export` (Draft Flattened SVG) als
  Geometriebasis. Kreise, die als `<circle>` ankommen, werden danach in eigene
  geschlossene `<path>`-Elemente aus vier Kreisbögen umgewandelt; Kreisbögen
  bleiben SVG-Bogenbefehle.
- Die Verschachtelungsprüfung wertet geschlossene `M`/`L`/`A`-Pfade und
  `<circle>`-Elemente aus. Andere SVG-Pfadsyntax wird bei dieser Prüfung
  übersprungen.
- Die Nachbearbeitung verkettet einzelne lineare SVG-Segmente (`L`) und
  Kreisbögen (`A`) anhand ihrer Endpunkte, auch über mehrere ursprüngliche
  `<path>`-Elemente hinweg. Segmente dürfen bei Bedarf umgekehrt werden;
  Kreisbögen behalten ihren Kreisbogenbefehl. SVG-Bogenflags müssen beim
  Serialisieren ganzzahlig als `0` oder `1` ausgegeben werden, nie als `0.0`
  oder `1.0`. Ein kleingeschriebenes `z` kommt
  nur ans Ende einer vollständigen Kontur, wenn deren Endpunkt innerhalb der
  kleinen Koordinatentoleranz wieder am Anfang liegt. Beim Einrasten wird nur
  der bereits vorhandene Endpunkt angepasst; es darf keine neue Schlusslinie
  entstehen. Segmente, die keinen geschlossenen Zyklus bilden, bleiben offene
  Pfade und werden als graue On-line-Schnitte ausgegeben.
- Nach dem Aufbau und der Prüfung geschlossener Konturen bleibt jede Kontur in
  einem eigenen `<path>`-Element. Es werden keine Konturen in ein gemeinsames
  Element übertragen.
- Geschlossene `M`/`L`/`A`-Pfade und `<circle>`-Elemente werden für die
  Verschachtelungsprüfung angenähert als Polygone abgetastet. Eine Kontur gilt
  als innenliegend, wenn ihr erster Konturpunkt innerhalb mindestens einer
  anderen Kontur liegt. Das ist eine Punkt-in-Polygon-Näherung; Transformationen
  verschiedener SVG-Gruppen werden dabei nicht berücksichtigt.
- Die Abschlussstatistik zählt exportierte `<path>`- und `<circle>`-Elemente
  gemeinsam als Konturen und gibt diese Anzahl statt der Anzahl selektierter
  FreeCAD-Objekte aus.
- Außenkonturen erhalten `fill="#000000"` und `stroke="#000000"`;
  innenliegende Konturen `fill="#ffffff"` und `stroke="#000000"`;
  Taschen `fill="#777777"` und `stroke="none"`. Die Klassifizierung bestimmt
  Taschen anhand der konfigurierten Maximalgröße und Innenkonturen anhand einer
  Punkt-in-Polygon-Näherung. Alle Konturen erhalten `stroke-width="0.01"` in SVG-Benutzereinheiten; da
  `viewBox` und reale SVG-Abmessungen denselben Zahlenbereich in mm verwenden,
  entspricht dies 0,01 mm. Die Breite wird auch bei `stroke="none"` gesetzt,
  damit Inkscape sie als 0,01 mm anzeigt. Shaper verwendet diese Breite nicht zur Bestimmung des
  Fräserdurchmessers. Attribute und gleichnamige Inline-Style-Eigenschaften
  werden aktualisiert.
- Aktuelle Toleranz zum Verbinden vorhandener SVG-Endpunkte: `0.001` SVG-Einheiten.
- Konturelemente bleiben getrennt; verbundene Segmente einer Kontur werden in
  einem eigenen Element ausgegeben.
- Punkte auf den exportierten Linienzügen werden per Ramer-Douglas-Peucker mit
  einer maximalen Abweichung von 0,01 mm vereinfacht. Dadurch verschwinden
  redundante Geradezwischenpunkte und Kurven erhalten weniger Segmente.
- Endpunkte werden ohne Toleranz verglichen. Nur exakt gleiche XY-Koordinaten
  werden verbunden.
- Die Überlappungsstatistik zählt Objektpaare mit sich schneidenden XY-
  Begrenzungsboxen. Berührende Boxen zählen als überlappend. Das ist eine
  Bounding-Box-Näherung und kein exakter Kontur- oder Flächenschnitt.
- Objekte ohne Shape-Kanten liefern keine SVG-Geometrie.
- Die Draft-Menüintegration und die FreeCAD-API-Aufrufe müssen in der
  installierten FreeCAD-Version zur Laufzeit geprüft werden; in dieser
  Arbeitsumgebung wurde FreeCAD nicht gestartet.

## Shaper-Origin-SVG-Anforderungen und Abgleich

Shaper veröffentlicht in den unten verlinkten Hilfeseiten praktische
Exportanforderungen und Farbcodierungen, aber kein vollständiges formales
SVG-Schema. Der Exporter ist gegen diese dokumentierten Anforderungen geprüft:

- **Vektordatei, reale Einheiten und RGB:** Origin erwartet SVG-Vektorgeometrie.
  Shaper empfiehlt reale Einheiten wie `mm` oder `inch` und RGB-Farben. Der
  Exporter schreibt `width`/`height` in Millimetern, eine dazu passende
  `viewBox` und RGB-Hexfarben.
- **Farbcodierte Schnittarten:** Schwarz gefüllt mit schwarzer Kontur kodiert
  Außenkonturen; weiß gefüllt mit schwarzer Kontur kodiert Innenkonturen; eine
  graue Füllung kodiert Taschen. Der Exporter verwendet diese Zuordnungen.
  Gleiche RGB-Kanäle ergeben Grau. Die Deckkraft muss 100 % betragen, damit die
  Graukodierung eindeutig bleibt.
- **Geschlossene Konturen:** Innen-, Außen- und Taschenschnitte müssen geschlossene
  Formen sein, damit Origin innen und außen unterscheiden kann. Exportierte
  Konturen enden mit kleingeschriebenem `z`; importierte Kreise werden als
  geschlossene Pfade aus vier SVG-Kreisbögen ausgegeben.
- **Offene Konturen:** Shaper erlaubt offene Formen für Führungs- und
  On-line-Schnitte. Der Exporter erhält offene Linienzüge ohne Schlusslinie und
  kodiert sie als graue On-line-Schnitte. Blaue Führungsschnitte bietet er
  weiterhin nicht als auswählbare Schnittart an.
- **Schnittartenwahl:** Die Zuordnung ist eine geometrische Heuristik, keine
  explizite Auswahl pro Kontur: Konturen unterhalb der Maximalgröße werden als
  Taschen behandelt; weitere verschachtelte Konturen als Innenkonturen. Vor dem
  Fräsen muss die auf Origin angezeigte Schnittart geprüft werden.
- **Deckkraft und Strichbreite:** Der Exporter setzt Deckkraft-, Fülldeckkraft-
  und Konturdeckkraftwerte auf 100 %, einschließlich ihrer Werte in Inline-
  Styles und SVG-Gruppen. Shaper sagt, dass die SVG-Strichbreite die Fräsbreite
  nicht bestimmt; der Exporter setzt `stroke-width` nur als Darstellungsattribut.
- **Schnitttiefe:** `shaper:cutDepth` ist eine optionale erweiterte SVG-Kodierung
  für die Schnitttiefe. Der Exporter schreibt sie nicht; die Tiefe wird auf
  Origin festgelegt.
- **Plain SVG und Transformationen:** Shaper empfiehlt für Inkscape Plain SVG,
  da anwendungsspezifische Fremddaten von Origin nicht verwendet werden. Der
  Exporter serialisiert Standard-SVG mit SVG-Geometrieelementen und einer
  Gruppen-Transformation für die Y-Achsenumkehr. Ein realer Import auf Origin
  wurde in dieser Arbeitsumgebung nicht durchgeführt.

### Quellen

- [SVG-Dateien Grundlagen (Shaper, Deutsch)](https://support.shapertools.com/de/hc/de-de/articles/115002735274-svg-dateien-grundlagen)
- [Cut Type Encoding (Shaper)](https://support.shapertools.com/hc/en-us/articles/115002721473-Cut-Type-Encoding)
- [Cut Types (Shaper)](https://support.shapertools.com/hc/en-us/articles/115002894673-cut-types)
- [Origin Readable Gray (Shaper)](https://support.shapertools.com/hc/en-us/articles/1260801903470-Create-an-Origin-Readable-Gray-in-Inkscape)
- [SVG-Dateien aus Inkscape (Shaper, Deutsch)](https://support.shapertools.com/de/hc/de-de/articles/360043458934-svg-dateien-aus-inkscape)
- [Manuelle SVG-Schnitttiefen-Kodierung (Shaper)](https://support.shapertools.com/hc/en-us/articles/12946815194011-manual-svg-cut-depth-encoding)
- [Loaded Design Has No Elements (Shaper)](https://support.shapertools.com/hc/en-us/articles/360005459814-loaded-design-has-no-elements-error-message)

## Pflege dieser Datei

- Diese `AGENTS.md` ist die maßgebliche, fortlaufend zu pflegende Beschreibung
  der Projektanforderungen und Nutzer-Vorgaben.
- Wenn der Nutzer Anforderungen, Einschränkungen, Verhaltensweisen oder
  gewünschte Ausgabeformate ergänzt oder ändert, ist diese Datei im selben
  Arbeitsgang entsprechend anzupassen. Die Dokumentation muss danach den
  aktuellen Vorgaben und dem tatsächlichen Implementierungsstand entsprechen.
- Bei Änderungen am Code sind betroffene Aussagen hier ebenfalls zu
  aktualisieren; veraltete oder widersprüchliche Vorgaben werden korrigiert.

## Lizenz

- Die Datei `LICENSE` enthält den vollständigen Text der GNU General Public License Version 3 (**GPL-3.0-only**).
