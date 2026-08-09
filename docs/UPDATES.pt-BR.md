# Manifesto de atualização

O IMAP Exporter consulta o seguinte documento HTTPS uma vez a cada abertura:

```text
https://raw.githubusercontent.com/ehstbr/IMAP-Exporter/main/version.json
```

A consulta automática é silenciosa e ocorre em segundo plano, sem painel de
startup, spinner ou bloqueio temporário da interface. O mesmo serviço atende à
ação **Verificar atualizações** da janela Sobre, onde o progresso permanece
visível porque a consulta foi solicitada explicitamente pelo usuário. Ele não
usa a API do GitHub, não interpreta a página da release, não baixa pacotes e
não executa comandos de instalação.

## Schema 1

```json
{
  "schema_version": 1,
  "version": "0.5.2",
  "mandatory": false,
  "released_at": "2026-08-09T23:35:23Z",
  "summary": "Resumo curto da versão em texto puro.",
  "changelog": [
    "Primeira alteração em texto puro.",
    "Segunda alteração em texto puro."
  ]
}
```

- `schema_version` deve ser o inteiro `1`.
- `version` deve ser uma versão semântica válida, sem o prefixo `v`.
- `mandatory` deve ser um booleano JSON, nunca texto ou número.
- `released_at` deve ser um timestamp ISO 8601 com fuso horário.
- `summary` é o resumo exibido imediatamente.
- `changelog` é a lista completa e ordenada exibida sob demanda.

O texto remoto é renderizado como texto puro. JSON inválido, campos ausentes,
schema desconhecido, tipos incorretos, erros HTTP, redirecionamento para fora
de HTTPS, timeout e resposta excessiva são falhas da verificação. O startup é
fail-open: a interface fica disponível imediatamente e o aplicativo continua
funcionando quando não consegue validar a política atual.

## Versões opcionais e obrigatórias

Quando a versão remota é mais nova e `mandatory` é `false`, o uso normal é
liberado e um aviso não modal é apresentado. Fechá-lo significa continuar com
a versão instalada durante aquela sessão.

Quando a versão remota é mais nova e `mandatory` é `true`, novas operações são
bloqueadas. Uma operação crítica que já esteja em execução pode alcançar um
ponto seguro; ela não é encerrada à força. O usuário pode abrir a release
oficial ou sair. Fechar a janela obrigatória nunca desbloqueia o aplicativo.

`mandatory: true` transforma, na prática, a versão publicada na versão mínima
permitida. Utilize somente para incompatibilidade ou correção de segurança
realmente crítica.

## Ordem segura de publicação

1. Atualize a versão canônica em `mail_exporter/__init__.py`.
2. Atualize changelog, documentação, metadados do pacote e `version.json` no
   candidato à release.
3. Execute todos os testes e gere o `.deb` e o ZIP finais.
4. Calcule novos hashes SHA-256 dos artefatos finais.
5. Crie a release no GitHub e envie os artefatos.
6. Confirme que `/releases/latest` abre a release publicada.
7. Somente então publique o `version.json` final na branch `main`.
8. Confirme que a URL raw retorna HTTP 200 e o JSON pretendido.

Nunca publique um manifesto obrigatório antes de disponibilizar a release e os
artefatos utilizáveis. O destino público para download é:

```text
https://github.com/ehstbr/IMAP-Exporter/releases/latest
```
