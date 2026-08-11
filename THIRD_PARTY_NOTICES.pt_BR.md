AVISOS DE COMPONENTES E DEPENDÊNCIAS

IMAP Exporter
Copyright (c) 2026 Eduardo Henrique Silva Teixeira
Licença: GNU General Public License versão 3

O código do IMAP Exporter, os ícones próprios distribuídos em assets/icons e
os demais recursos criados especificamente para o projeto são disponibilizados
sob a GNU GPLv3 reproduzida na aba “Licença” e no arquivo LICENSE.

DEPENDÊNCIAS DO SISTEMA

O pacote Debian não incorpora cópias das bibliotecas abaixo. Ele solicita ao
gerenciador de pacotes que use as versões instaladas pelo sistema operacional.
Cada componente conserva seus próprios direitos autorais e termos:

• Python 3 — interpretador usado para executar o aplicativo.
  Licença: Python Software Foundation License.
  Referência: https://docs.python.org/3/license.html

• GTK 4 — biblioteca da interface gráfica.
  Licença do projeto GTK: GNU Lesser General Public License,
  versão 2.1 ou posterior.
  Referência: https://www.gtk.org/docs/legal/

• PyGObject / python3-gi — integração entre Python e GTK.
  Licença do projeto: GNU Lesser General Public License,
  versão 2.1 ou posterior.
  Referência: https://pygobject.gnome.org/

• OpenSSL — operações criptográficas usadas para proteger a credencial IMAP.
  A licença exata acompanha a versão instalada pelo sistema.
  Referência: https://www.openssl.org/source/license.html

• Hicolor Icon Theme — infraestrutura de descoberta de ícones do ambiente
  desktop. O IMAP Exporter não redistribui ícones de terceiros: os ícones do
  aplicativo incluídos no pacote são próprios e usam a GNU GPLv3.
  Referência: https://www.freedesktop.org/wiki/Software/icon-theme/

COMO CONSULTAR OS TEXTOS INSTALADOS

Em Debian, Ubuntu e derivados, os avisos completos da versão efetivamente
instalada normalmente ficam em:

/usr/share/doc/python3/copyright
/usr/share/doc/python3-gi/copyright
/usr/share/doc/libgtk-4-1/copyright
/usr/share/doc/openssl/copyright
/usr/share/doc/hicolor-icon-theme/copyright

Os nomes e caminhos podem variar conforme a distribuição. Este aviso não
substitui os textos de licença fornecidos por cada projeto ou pacote.
