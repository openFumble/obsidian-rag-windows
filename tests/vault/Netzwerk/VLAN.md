---
type: wissen
tags: [netzwerk, grundlagen]
aliases:
  - Virtual LAN
---

# VLAN

**Kurzfassung:** Ein VLAN teilt ein physisches Netzwerk in mehrere logisch getrennte Netze. Geräte in verschiedenen VLANs erreichen sich nur über einen Router oder Layer-3-Switch.

## Wie es funktioniert
- Arbeitet auf Schicht 2 des OSI-Modells
- Jedes VLAN hat eine VLAN-ID von 1 bis 4094
- Der Standard IEEE 802.1Q fügt dem Ethernet-Rahmen ein Tag mit der VLAN-ID hinzu

## Untagged und Tagged
Ein untagged Port gehört zu genau einem VLAN. Ein tagged Port (Trunk) transportiert mehrere VLANs, zum Beispiel zu Access Points.

```bash
# Kein Heading: Kommentar in einem Codeblock
show vlan brief
```

## Verwandt
- [[Router und Layer-3-Switch]]
- [[PoE]]
