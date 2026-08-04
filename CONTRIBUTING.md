# Como contribuir

Obrigado pelo interesse em melhorar o IMAP Exporter.

## Antes de começar

- Pesquise nas Issues para evitar relatos duplicados.
- Não publique senhas, endereços particulares, bancos de dados, mensagens ou
  outros dados reais de uma conta.
- Para falhas de segurança, siga `SECURITY.md` em vez de abrir uma Issue.

## Ambiente de desenvolvimento

Em Ubuntu, Debian e derivados:

```bash
sudo apt install python3 python3-gi gir1.2-gtk-4.0 openssl \
  ca-certificates hicolor-icon-theme pkexec
```

Execute o aplicativo:

```bash
./run.sh
```

Execute os testes:

```bash
python3 -m unittest discover -s tests -v
python3 -m json.tool locales/pt_BR.json >/dev/null
python3 -m json.tool locales/en.json >/dev/null
bash -n run.sh packaging/build-deb.sh
```

## Envio de alterações

1. Crie uma branch curta e descritiva.
2. Faça mudanças focadas em um único problema.
3. Mantenha português e inglês sincronizados nos arquivos de tradução.
4. Adicione ou atualize testes para mudanças de comportamento.
5. Confirme que a suíte completa passa.
6. Descreva no Pull Request o problema, a solução e como a alteração foi
   validada.

Mudanças que movimentem ou restaurem mensagens devem preservar as garantias de
segurança do projeto: nunca fazer `EXPUNGE` global e nunca considerar uma
operação concluída antes da confirmação do servidor.

## Estilo

- Use Python 3.10 ou superior.
- Preserve a compatibilidade com GTK 4.
- Prefira bibliotecas da distribuição e da biblioteca padrão.
- Evite novas dependências sem justificar a necessidade.
- Mantenha as linhas de interface curtas e os textos traduzíveis.
