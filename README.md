# Monitor Arautos do Evangelho — visão 360

Agente de coleta de publicações públicas e descoberta de vídeos e referências sobre a organização. Acompanha notícias, canais institucionais, índices públicos e páginas que citam mídia. A pesquisa ampliada começa com doze idiomas e mantém histórico, proveniência e links para conferência. Não requer chave de API nem pacote externo.

## Executar

Requisito: Python 3.10 ou superior em Linux, com SQLite, SSL e dados do fuso `America/Sao_Paulo`. Ambiente validado com Python 3.12.14.

```bash
cd /workspace/desenvolvi
python3 -m unittest discover -s tests -v
python3 monitor.py --once
```

Para o modo local de notícias a cada três horas, mantendo o processo em execução:

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
- `discovery-latest.md` e `discovery-latest.json`: resultados indexados e pistas da pesquisa mundial, com consultas, bloqueios e posição da fila.
- `catalogue-latest.md` e `catalogue-latest.json`: links audiovisuais extraídos de páginas públicas, incluindo arquivos diretos, incorporações e referências a conferir.

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

`sources.json` define 13 consultas de notícias e canais institucionais:

- Google News: seis edições de idioma, com nomes locais da organização e uma janela de busca de 90 dias; uma consulta adicional de assuntos jurídicos e de governança.
- Bing News: busca pelo nome em português; o retorno pode incluir publicações antigas, independentemente da janela usada no Google.
- Revista oficial: RSS da seção **Arautos no Mundo**, com as últimas edições disponíveis.
- Site oficial: notícias individuais da listagem `https://www.arautos.org/artigos/noticias`.
- Vatican News: RSS em português, inglês e espanhol, filtrado por menções aos nomes da organização e entidades relacionadas.

As buscas sociais antes concentradas em nove consultas foram incorporadas à fila mundial descrita abaixo. TikTok, X/Twitter e Vimeo continuam nos escopos dessa fila, junto com outras plataformas.

Uma consulta pode funcionar e retornar zero itens. A pesquisa textual filtra títulos e trechos disponíveis nos feeds; uma referência presente apenas no corpo de um artigo pode não ser incluída. A edição de busca não garante o idioma de cada texto. Os termos e as fontes podem ser ampliados no arquivo de configuração; esta seleção não representa todos os idiomas ou todas as páginas da internet.

O painel mostra itens encontrados, e não um cadastro completo da organização. Não há consulta integral de processos, DOU, registros financeiros, redes sociais ou bases fechadas. Documentos não indexados e conteúdo com autenticação dependem de conectores próprios. Contagem zero em um tema significa apenas que não houve correspondência no material coletado.

A deduplicação usa a URL normalizada, remove rastreamento conhecido e resolve links intermediários do Bing. O mesmo conteúdo publicado em endereços diferentes, inclusive em agregadores diferentes, ainda pode aparecer mais de uma vez.

## Ajustar o monitor

Edite `sources.json` para mudar `interval_seconds` (mínimo de 60 segundos), os termos de notícias, as edições ou as fontes RSS/Atom. Exemplos de execução com caminhos separados:

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

Os testes locais verificam RSS/Atom, relevância, deduplicação persistente, falhas de fontes, segurança dos dados no painel, fila de consultas, orçamento de tempo, bloqueios e extração de catálogos. A coleta real depende da rede e deve ser verificada separadamente.

O código foi criado neste checkout. Não há necessidade de worktree: cada tarefa na nuvem já possui um ambiente isolado. As instruções de inicialização salvas nas configurações do ambiente explicam como verificar e reiniciar o processo após restaurar a máquina. Não é necessário instalar dependências com `pip` ou `npm`.

## Executar sem VPS ou aba aberta

O fluxo `.github/workflows/monitor.yml` usa GitHub Actions para executar uma rodada independente a cada três horas. O cron `17 */3 * * *` corresponde a **00:17, 03:17, 06:17, 09:17, 12:17, 15:17, 18:17 e 21:17 em São Paulo**. A primeira execução também é acionada ao publicar o código em `main`; há acionamento manual em **Actions → Monitor Arautos a cada 3 horas → Run workflow**.

Após publicar o fluxo na branch padrão e confirmar que Actions está habilitado, o GitHub inicia as execuções mesmo com o computador e a conversa fechados. Manter uma aba aberta não mantém este ambiente Codex ativo. O cron do GitHub pode atrasar ou perder uma execução em períodos de carga; não fornece garantia de horário exato.

Cada rodada executa os testes, a coleta de notícias, um lote da descoberta mundial e a extração de catálogos. Baixe o artefato **monitor-relatorios-…** na página da execução: contém painel HTML, relatórios JSON/Markdown e cópias dos três bancos. Os artefatos são retidos por até 90 dias, sujeitos às configurações do repositório. Não há envio automático de mensagens ou e-mails pelo agente.

Os bancos são reutilizados por cache entre execuções e também incluídos no artefato. A migração do cache mantém o histórico de notícias existente e passa a preservar também descoberta e catálogos. O cache pode ser removido pelo GitHub; nesse caso o acervo pode ser reconstruído, e itens antigos serão considerados novos. Execuções parciais mantêm seu código de saída e aparecem como falha no GitHub, mas as outras etapas, relatórios e estado são preservados e a próxima rodada continua programada. Consulte os erros em cada relatório; um fluxo que executou não significa que todas as fontes responderam.

Este repositório é público, e o fluxo usa runners padrão. Não precisa de VPS nem de uma chave adicional de busca; aplicam-se as regras e disponibilidade do GitHub Actions. Em repositórios públicos, workflows agendados podem ser desativados após 60 dias sem atividade no repositório e precisar de reativação. O fluxo não altera as regras de acesso das redes: páginas privadas, exigências de login e desafios continuam fora do coletor.

O modo local `--watch` usa cadência fixa de 10.800 segundos sem somar a duração da consulta ao intervalo e registra a próxima tentativa em `.monitor/state.sqlite3.schedule.json`. No GitHub, cada execução usa somente `--once`; o agendamento é feito pelo serviço Actions.

## Descoberta mundial incremental

`discovery_queries.json` contém **336 entradas de consulta**: doze idiomas, sete escopos e quatro provedores. Idiomas iniciais: português, inglês, espanhol, francês, italiano, alemão, polonês, russo, árabe, japonês, chinês e neerlandês. São uma seleção inicial; não representam todos os idiomas existentes.

Os escopos incluem web aberta, redes principais, redes e fóruns, plataformas alternativas, blogs independentes, vídeos/arquivos públicos e outros locais de hospedagem indexados. Entre os alvos estão YouTube, Facebook, Instagram, TikTok, X/Twitter, Reddit, Telegram público, OK.ru, VK, Rutube, Rumble, Odysee, Vimeo, Dailymotion, Archive.org, BitChute e PeerTube. Há também consultas textuais aos índices de locais adultos solicitados pelo usuário; nenhum vídeo é baixado ou reproduzido pelo coletor.

Os provedores são DuckDuckGo HTML, Bing HTML, Google News RSS e Bing News RSS. Os dois últimos têm cobertura de notícias e não substituem um índice completo da web. Suas URLs podem apontar ao agregador em vez do vídeo original. Todos os resultados preservam provedor e consulta de origem.

```bash
python3 discovery.py --once --max-queries 48 --max-seconds 1200 --spacing 4
python3 catalogue.py --once --spacing 4
```

Cada rodada percorre até 48 entradas, com intervalo mínimo de quatro segundos entre pedidos ao mesmo host e até vinte minutos de orçamento para a descoberta. O cursor é salvo em `.monitor/discovery.sqlite3`, e a próxima execução continua a fila. Uma rotação da matriz tem sete lotes nominais; consultas puladas por bloqueio são identificadas como não executadas. Percorrer a fila não comprova que todos os alvos foram pesquisados com sucesso.

Desafios, recusas ou limites suspendem o provedor afetado naquela rodada. Duas respostas HTML consecutivas sem formato ou relevância utilizáveis também pausam esse provedor. Outros índices seguem sendo consultados. Mais tempo não elimina esses impedimentos; os relatórios conservam as razões.

Um resultado com vínculo ao nome e ao tema é marcado **indexado**; uma conexão parcial é marcada **pista**, para conferência humana. Em índices de notícias, uma pista temática pode ter associação ao nome apenas pelos termos da consulta; essa limitação fica explícita junto ao link. Ambos podem estar sem reprodução confirmada. Resultados alheios sem conexão suficiente são descartados. Links diferentes podem conter a mesma gravação e não são apresentados como filmagens inéditas.

O catálogo separado acessa três páginas públicas conhecidas e extrai seus links e incorporações, mesmo quando os índices estão bloqueados. Mantém `.monitor/catalogues.sqlite3`. Descrições dos próprios publicadores são preservadas; conteúdos sobre reuniões, depoimentos ou outros assuntos permanecem identificados para conferência.

O workflow tem limite total de 45 minutos. A continuidade é obtida por rodadas e estado persistente, não por uma requisição sem limite. A pesquisa não acessa contas privadas, não contorna CAPTCHA/login e não garante cobertura integral da internet.
