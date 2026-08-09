# Pacote Debian

O pacote `.deb` instala o aplicativo em `/usr/lib/imap-exporter`, cria o
comando `imap-exporter` e adiciona o atalho ao menu de aplicativos.

Para construir:

```bash
./packaging/build-deb.sh
```

O arquivo será criado em `dist/`. Para instalar com resolução automática das
dependências:

```bash
sudo apt install ./dist/imap-exporter_0.5.0_all.deb
```

O pacote é independente de arquitetura e foi preparado para Debian, Ubuntu,
Lubuntu, Linux Mint e outras distribuições que usem pacotes Debian e ofereçam
GTK 4. Outros formatos de pacote exigem um processo de empacotamento próprio.
