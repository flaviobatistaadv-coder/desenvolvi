# Monitor Arautos do Evangelho — visão 360

Agente de coleta de publicações públicas sobre a organização. Pesquisa nomes em português, inglês, espanhol, francês, italiano e alemão, acompanha fontes institucionais e produz um painel local com histórico, filtros e links. Não requer chave de API nem pacote externo.

## Executar

Requisito: Python 3.10 ou superior em Linux, com SQLite, SSL e dados do fuso `America/Sao_Paulo`. Ambiente validado com Python 3.12.14.

```bash
cd /workspace/desenvolvi
python3 -m unittest discover -s tests -v
python3 monitor.py --once
```

Para consultar a cada três horas, mantendo o processo em execução:

```bash
python3 -u monitor.py --watch
```

Interrompa com `Ctrl+C`. O histórico fica preservado no disco. Não inicie dois processos com o mesmo banco: uma trava impede sobreposição, inclusive entre `--watch` e `--once`.

Uma máquina ou sessão Codex pode ser encerrada. Publicar o ambiente salva seu estado preparado; processos precisam ser iniciados novamente. Monitoramento permanente exige uma máquina ou um agendador que permaneça disponível. Esta versão grava resultados locais; não envia mensagens nem possui integração de e-mail, WhatsApp ou Telegram.

## Consultar resultados

Após a coleta, os arquivos ficam em `.monitor/reports/`:

- `painel.html`: painel independente, com pesquisa e filtros por tema, edição e origem. Pode ser aberto como arquivo HTML; não precisa de servidor.
- `latest.md`: situação das fontes, novidades e índice de todo o histórico.
- `latest.json`: dados estruturados, inclusive fontes, datas e marcadores de novidades.
- `history/`: relatório Markdown de cada execução.

O banco `.monitor/state.sqlite3` mantém itens, fontes que os encontraram e histórico das execuções. Preserve `.monitor/` para manter a deduplicação e o acervo; essa pasta é ignorada pelo Git. Os relatórios são retratos da última coleta: recarregar o HTML não consulta a internet.

## O que é acompanhado

- Jurídico: menções a investigações, tribunais, denúncias e decisões em publicações disponíveis nas fontes.
- Governança e Igreja: dirigentes, fundação, relações com a Igreja e o Vaticano.
- Educação: colégios, formação, alunos e atividades de ensino.
- Finanças e patrimônio: referências públicas a doações, orçamento e bens.
- Presença internacional: atividades e repercussão em outros países.
- Atividades e eventos: missas, missões, visitas, campanhas e concertos.
- Comunicação e cultura: revistas, vídeos, livros, entrevistas e produções culturais.
- Institucional: notícias e publicações dos canais da própria organização.

Os temas são atribuídos por palavras-chave e podem ter falsos positivos. Uma publicação pode pertencer a mais de um tema. A categoria não é uma análise jurídica ou financeira. Títulos e trechos são atribuídos aos seus veículos; o agente não verifica alegações nem traduz os textos.

## Fontes e alcance

`sources.json` define 13 consultas de notícias e canais institucionais, além de 9 buscas sociais:

- Google News: seis edições de idioma, com nomes locais da organização e uma janela de busca de 90 dias; uma consulta adicional de assuntos jurídicos e de governança.
- Bing News: busca pelo nome em português; o retorno pode incluir publicações antigas, independentemente da janela usada no Google.
- Revista oficial: RSS da seção **Arautos no Mundo**, com as últimas edições disponíveis.
- Site oficial: notícias individuais da listagem `https://www.arautos.org/artigos/noticias`.
- Vatican News: RSS em português, inglês e espanhol, filtrado por menções aos nomes da organização e entidades relacionadas.
- TikTok, X/Twitter e Vimeo: buscas via RSS no índice público do Bing, com região e idioma explícitos para português/Brasil, espanhol/Espanha e inglês/Estados Unidos. O adaptador do DuckDuckGo permanece disponível para configuração manual, mas não é usado pelas fontes padrão por retornar HTTP 202 nas execuções do GitHub Actions. Só entram URLs específicas de vídeos/posts com correspondência simultânea ao nome e ao tema. A indexação não confirma reprodução nem autoria. Resposta 202, CAPTCHA, RSS inválido ou resultados sem relação com a consulta geram erro explícito. Um RSS válido sem itens representa ausência de resultados naquele índice, não ausência de publicações nas redes.

Uma consulta pode funcionar e retornar zero itens. A pesquisa textual filtra títulos e trechos disponíveis nos feeds; uma referência presente apenas no corpo de um artigo pode não ser incluída. A edição de busca não garante o idioma de cada texto. Os termos e as fontes podem ser ampliados no arquivo de configuração; esta seleção não representa todos os idiomas ou todas as páginas da internet.

O painel mostra itens encontrados, e não um cadastro completo da organização. Não há consulta integral de processos, DOU, registros financeiros, redes sociais ou bases fechadas. Documentos não indexados e conteúdo com autenticação dependem de conectores próprios. Contagem zero em um tema significa apenas que não houve correspondência no material coletado.

A deduplicação usa a URL normalizada, remove rastreamento conhecido e resolve links intermediários do Bing. O mesmo conteúdo publicado em endereços diferentes, inclusive em agregadores diferentes, ainda pode aparecer mais de uma vez.

## Ajustar o monitor

Edite `sources.json` para mudar `interval_seconds` (mínimo de 60 segundos), os termos de busca, as edições ou as fontes RSS/Atom. Exemplos de execução com caminhos separados:

```bash
python3 monitor.py --once --config sources.json --state .monitor/state.sqlite3 --output-dir .monitor/reports
python3 monitor.py --watch --interval 10800
```

Cada fonte tem `id` único, `name`, `url` HTTPS, `kind` e `language`. `match_terms` é uma lista opcional de expressões: pelo menos uma deve ocorrer no título ou trecho; a comparação ignora maiúsculas e acentos. Fontes institucionais específicas dispensam esse filtro. A opção `format: "official_news_html"` é um adaptador específico da listagem do site oficial, não um extrator de qualquer página.

Uma fonte falhar não impede a gravação dos resultados das demais. Erros aparecem no relatório e nos logs. Se a estrutura da listagem oficial mudar, o adaptador sinaliza erro em vez de assumir uma coleta vazia bem-sucedida.

Saídas de `--once`: **0** = todas as consultas concluídas; **2** = coleta parcial; **1** = todas as fontes falharam, configuração inválida ou falha local. A primeira execução serve de base e considera novos todos os itens encontrados. Novidade significa primeira detecção pelo monitor, e não necessariamente publicação recente.

As datas de feeds com fuso são normalizadas para UTC no JSON e exibidas no fuso de São Paulo no painel. A listagem oficial não informa fuso; dela é mantida somente a data de calendário, sem inventar horário.

## Desenvolvimento e reinício

```bash
python3 -m unittest discover -s tests -v
python3 monitor.py --once
```

Os testes locais verificam RSS/Atom, relevância, deduplicação persistente, falhas de fontes, segurança dos dados no painel e o adaptador HTML oficial. A coleta real depende da rede e deve ser verificada separadamente.

O código foi criado neste checkout. Não há necessidade de worktree: cada tarefa na nuvem já possui um ambiente isolado. As instruções de inicialização salvas nas configurações do ambiente explicam como verificar e reiniciar o processo após restaurar a máquina. Não é necessário instalar dependências com `pip` ou `npm`.

## Executar sem VPS ou aba aberta

O fluxo `.github/workflows/monitor.yml` usa GitHub Actions para executar uma rodada independente a cada três horas. O cron `17 */3 * * *` corresponde a **00:17, 03:17, 06:17, 09:17, 12:17, 15:17, 18:17 e 21:17 em São Paulo**. A primeira execução também é acionada ao publicar o código em `main`; há acionamento manual em **Actions → Monitor Arautos a cada 3 horas → Run workflow**.

Após publicar o fluxo na branch padrão e confirmar que Actions está habilitado, o GitHub inicia as execuções mesmo com o computador e a conversa fechados. Manter uma aba aberta não mantém este ambiente Codex ativo. O cron do GitHub pode atrasar ou perder uma execução em períodos de carga; não fornece garantia de horário exato.

Cada rodada executa os testes e a coleta. Baixe o artefato **monitor-relatorios-…** na página da execução: contém painel HTML, relatórios JSON/Markdown e cópia do banco. Os artefatos são retidos por até 90 dias, sujeitos às configurações do repositório. Não há envio automático de mensagens ou e-mails pelo agente.

O banco é reutilizado por cache entre execuções e também incluído no artefato. O cache pode ser removido pelo GitHub; nesse caso o acervo pode ser reconstruído, e itens antigos serão considerados novos. Execuções parciais mantêm seu código de saída e aparecem como falha no GitHub, mas os relatórios e o estado são preservados e a próxima rodada continua programada. Consulte os erros por fonte em `latest.json`; um fluxo que executou não significa que as redes responderam.

Este repositório é público, e o fluxo usa runners padrão. Não precisa de VPS nem de uma chave adicional de busca; aplicam-se as regras e disponibilidade do GitHub Actions. Em repositórios públicos, workflows agendados podem ser desativados após 60 dias sem atividade no repositório e precisar de reativação. O fluxo não altera as regras de acesso das redes: páginas privadas, exigências de login e desafios continuam fora do coletor.

O modo local `--watch` usa cadência fixa de 10.800 segundos sem somar a duração da consulta ao intervalo e registra a próxima tentativa em `.monitor/state.sqlite3.schedule.json`. No GitHub, cada execução usa somente `--once`; o agendamento é feito pelo serviço Actions.
