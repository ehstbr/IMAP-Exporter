# Histórico de alterações

Todas as mudanças relevantes do IMAP Exporter serão registradas neste arquivo.
O projeto utiliza versões no formato `MAJOR.MINOR.PATCH`.

## [0.5.0] - 2026-08-09

### Atualizações

- Adicionada uma verificação automática por abertura baseada no arquivo
  `version.json` do repositório oficial no GitHub.
- A janela Sobre ganhou a ação manual `Verificar atualizações`, com retorno
  visível para versão atual, falha de rede e instalação mais nova que a release.
- Atualizações opcionais são apresentadas em janela não modal e permitem
  continuar usando a versão instalada durante a sessão.
- Atualizações obrigatórias bloqueiam novas operações pelo estado do aplicativo,
  preservam a conclusão segura de tarefas críticas já iniciadas e não podem ser
  ignoradas pelo botão de fechar ou pela tecla Escape.
- Resumo da versão permanece visível e o changelog completo começa recolhido,
  pode ser expandido e usa rolagem para listas longas.
- Startup e verificação manual compartilham o mesmo serviço, coalescendo cliques
  simultâneos e impedindo janelas duplicadas para a mesma release.
- Falhas de rede, timeout, HTTP, JSON, schema ou versão usam política fail-open
  no startup e nunca tornam o GitHub um requisito para abrir o aplicativo.
- Adicionadas validação rígida do manifesto, comparação SemVer, limite de
  resposta, redirects somente por HTTPS e User-Agent sem dados pessoais.
- O verificador somente abre a release oficial; não baixa pacotes nem executa
  `sudo`, `apt`, `dpkg` ou instalação silenciosa.

### Projeto e distribuição

- Versão canônica centralizada em `mail_exporter/__init__.py` e reutilizada pela
  interface, pelo verificador e pelo gerador do pacote Debian.
- Adicionados `version.json`, documentação bilíngue para mantenedores e testes
  automatizados do contrato, das políticas e do lifecycle do updater.

## [0.4.13] - 2026-08-03

### Compatibilidade e internacionalização

- Removida a reutilização automática da pasta de dados do nome antigo do
  aplicativo. O programa agora usa somente `~/.local/share/imap-exporter`.
- Removidos os aliases antigos de variável de ambiente, nome de banco,
  migrações de esquema e formato de credencial legado.
- O script de execução do código-fonte foi renomeado de `executar.sh` para
  `run.sh` e suas mensagens foram padronizadas em inglês.
- Textos dinâmicos de interface, erros, progresso, exportações CSV/ODS e dados
  de provedores passaram a respeitar integralmente o idioma selecionado.
- Adicionados testes de regressão para impedir a reutilização de dados antigos
  e detectar textos sem tradução no modo inglês.

## [0.4.12] - 2026-08-03

### Interface

- Corrigida no LXQt/Lubuntu a interseção de 1 px entre os separadores das
  linhas de anexos e a borda dos botões de download. O conteúdo sobreposto
  agora participa do cálculo da altura da linha e fica limitado à sua área,
  preservando o mesmo resultado no GNOME e em temas com botões mais altos.

## [0.4.11] - 2026-08-02

### Interface

- O carregamento das pastas passou para dentro da área fixa da própria lista,
  sem deslocar as linhas quando a consulta termina.
- A janela de mensagens de remetentes e domínios ganhou filtro por intervalo
  de datas na mesma linha da busca textual.
- Uma indicação visível de duplo clique foi acrescentada junto ao contador da
  lista.
- A paginação agora informa quantas mensagens foram exibidas do total. O botão
  `Carregar mais` aparece em remetentes e domínios somente quando existe outra
  página; quando todos os resultados já foram carregados, isso é informado.
- O preenchimento do progresso na linha dos anexos ganhou uma cor de fallback
  compatível com temas que não fornecem a variável de destaque do GNOME.

## [0.4.10] - 2026-08-02

### Interface e downloads

- Log da análise complementar de anexos padronizado com os demais diálogos:
  botão compacto, conteúdo inicialmente recolhido e ação para copiar.
- Cada linha de anexo funciona como uma barra de progresso durante o download.
- `Baixar todos` atualiza individualmente a linha do arquivo atual e preserva
  o resultado das linhas já concluídas.
- O botão de uma linha em download muda para cancelar aquela transferência;
  no modo em lote, `Baixar todos` muda para `Cancelar todos`.
- O cancelamento remove arquivos temporários incompletos, preserva anexos já
  salvos e impede que os próximos downloads do lote sejam iniciados.
- Anexos grandes são solicitados ao servidor em blocos IMAP para produzir
  progresso real, mantendo `BODY.PEEK` e sem marcar a mensagem como lida.
- Corrigida a abertura da janela Sobre no GNOME após a remoção de seu rodapé;
  uma referência antiga ao botão Fechar impedia a janela de ser apresentada.

### Repositório

- README preparado para apresentação pública, com ícone, indicadores,
  instalação pelo código-fonte e links de suporte.
- Política de segurança, guia de contribuição, changelog, modelos de Issues e
  Pull Requests e integração contínua adicionados.
- Estrutura reservada para futuras capturas de tela sem dados pessoais.

## [0.4.9] - 2026-07-29

### Interface

- Ícone oficial do aplicativo adicionado à janela Sobre.
- Rodapé redundante removido das janelas Leitor de mensagem e Sobre.
- Controles nativos de maximização e restauração usados no leitor.
- Abas de resultados reorganizadas para priorizar a área das listas.
- Listas, áreas roláveis e cartões ajustados para manter quinas arredondadas.
- Resumo visual substitui o antigo bloco de texto selecionável.
- Textos informativos passaram a usar rótulos não selecionáveis.

### Sincronização e análise

- Sincronização incremental compara o estado atual do servidor com o índice
  local e baixa somente cabeçalhos desconhecidos.
- Estrutura MIME de mensagens novas coletada na mesma operação dos cabeçalhos.
- Aba Maiores adicionada com ranking por tamanho, anexos e extensões.
- Filtros de extensões permitem múltiplas seleções.
- Leitor leve e download sob demanda de anexos adicionados.
- Mini sincronização executada depois de movimentações e reversões.

### Limpeza e segurança

- Movimentação em lote ganhou janela própria, pausa, continuação, interrupção,
  log detalhado e reversão quando o servidor fornece identificadores seguros.
- Estado local é reconciliado depois de mover ou restaurar mensagens.
- Remoção de conta bloqueada pode ser autorizada administrativamente, sem abrir
  a credencial IMAP ou acessar o servidor.
- Ícone crítico diferencia a remoção administrativa da remoção comum.

### Distribuição

- Pacote Debian com dependências declaradas, integração ao menu, ícones,
  metadados, licença, avisos de terceiros e política Polkit.
- Interface em português e inglês por arquivos de tradução.
[0.5.0]: https://github.com/ehstbr/IMAP-Exporter/releases/tag/v0.5.0
[0.4.13]: https://github.com/ehstbr/IMAP-Exporter/releases/tag/v0.4.13
[0.4.12]: https://github.com/ehstbr/IMAP-Exporter/releases/tag/v0.4.12
[0.4.11]: https://github.com/ehstbr/IMAP-Exporter/releases/tag/v0.4.11
[0.4.10]: https://github.com/ehstbr/IMAP-Exporter/releases/tag/v0.4.10
[0.4.9]: https://github.com/ehstbr/IMAP-Exporter/releases/tag/v0.4.9
