COMPONENT AND DEPENDENCY NOTICES

IMAP Exporter
Copyright (c) 2026 Eduardo Henrique Silva Teixeira
License: MIT

The IMAP Exporter code, the original icons distributed under assets/icons,
and the other resources created specifically for the project are made
available under the MIT License reproduced in the “License” tab and in the
LICENSE file.

SYSTEM DEPENDENCIES

The Debian package does not embed copies of the libraries listed below. It
asks the package manager to use versions installed by the operating system.
Each component retains its own copyright notices and terms:

• Python 3 — interpreter used to run the application.
  License: Python Software Foundation License.
  Reference: https://docs.python.org/3/license.html

• GTK 4 — graphical user interface toolkit.
  Project license: GNU Lesser General Public License,
  version 2.1 or later.
  Reference: https://www.gtk.org/docs/legal/

• PyGObject / python3-gi — Python integration for GTK.
  Project license: GNU Lesser General Public License,
  version 2.1 or later.
  Reference: https://pygobject.gnome.org/

• OpenSSL — cryptographic operations used to protect the IMAP credential.
  The exact license is supplied with the version installed by the system.
  Reference: https://www.openssl.org/source/license.html

• Hicolor Icon Theme — desktop icon discovery infrastructure. IMAP Exporter
  does not redistribute third-party icons: the application icons included in
  the package are original and use the MIT License.
  Reference: https://www.freedesktop.org/wiki/Software/icon-theme/

HOW TO READ THE INSTALLED LICENSE TEXTS

On Debian, Ubuntu, and derivatives, the complete notices for the versions
actually installed are normally available at:

/usr/share/doc/python3/copyright
/usr/share/doc/python3-gi/copyright
/usr/share/doc/libgtk-4-1/copyright
/usr/share/doc/openssl/copyright
/usr/share/doc/hicolor-icon-theme/copyright

Package names and paths may vary by distribution. This notice does not replace
the license texts supplied by each project or package.
