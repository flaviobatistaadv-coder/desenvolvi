# Diagnóstico do workflow Monitor Arautos — 28/10/2026

## Resultado confirmado

A execução [40689613403](https://github.com/flaviobatistaadv-coder/desenvolvi/actions/runs/40689613403), no commit 0806ca83efcd592a538f7039e061d26367b2bc0e, falhou na etapa de coleta porque as nove buscas sociais no DuckDuckGo responderam HTTP 202. O programa registrou coleta parcial e retornou código 2, conforme a política documentada no README. Os 24 testes, a preservação do banco e a publicação do artefato concluíram com sucesso.

As execuções agendadas seguintes repetiram o problema. Na execução [40799569897](https://github.com/flaviobatistaadv-coder/desenvolvi/actions/runs/40799569897), o histórico tinha 39 itens e as mesmas nove fontes sociais falharam.

## Validação da alternativa Bing

A troca simples pelo HTML do Bing foi testada em [40805463688](https://github.com/flaviobatistaadv-coder/desenvolvi/actions/runs/40805463688). A resposta HTTP 200 continha resultados sem relação com as plataformas solicitadas e foi corretamente rejeitada.

A configuração explícita de país e idioma e o reconhecimento das mensagens localizadas de ausência de resultados passaram nos testes locais. A coleta local concluiu 22 consultas. No runner do GitHub, porém, [40805967276](https://github.com/flaviobatistaadv-coder/desenvolvi/actions/runs/40805967276) continuou parcial.

As amostras registradas em [40806269677](https://github.com/flaviobatistaadv-coder/desenvolvi/actions/runs/40806269677) mostram páginas com o título da consulta original e resultados alheios ao assunto. Exemplos incluem dicionários para consultas em inglês e páginas de finanças, eletricistas e outros temas para consultas em espanhol e português.

O RSS do Bing também foi testado. Localmente, as nove consultas entregaram RSS válido sem itens. No runner, [40806715264](https://github.com/flaviobatistaadv-coder/desenvolvi/actions/runs/40806715264) recebeu resultados alheios ao assunto nas nove fontes. Os 29 testes daquela tentativa passaram, mas a coleta permaneceu parcial. O formato RSS não resolveu a diferença entre ambientes.

## Estado deixado no repositório

- As fontes padrão foram restauradas ao DuckDuckGo, conforme a configuração original.
- Foram mantidos o reconhecimento de mensagens explícitas de ausência de resultados em português e espanhol e o diagnóstico limitado de respostas HTML com resultados alheios à plataforma.
- O adaptador experimental de RSS de busca foi removido.
- A classificação de coleta parcial, os relatórios, o histórico e a programação de três em três horas foram preservados.
- A suíte final contém 25 testes. Nenhum resultado alheio ao assunto foi incorporado ao histórico como notícia pertinente.

## Pendência para concluir a correção operacional

Ainda não foi obtida coleta social válida e repetível no runner. A substituição direta entre buscadores gratuitos não foi suficiente.

O próximo passo recomendado é integrar uma API de busca autenticada, preservando a validação de plataforma, organização e tema. Isso depende de uma credencial do usuário e da escolha do serviço, sem contratar ou comprar créditos automaticamente. Por exemplo, a [Brave Search API](https://api-dashboard.search.brave.com/documentation/guides/authentication) usa uma chave no cabeçalho X-Subscription-Token. A chave deve ser configurada como segredo do GitHub, nunca colocada no código ou nos relatórios.

A aprovação dos testes locais não comprova disponibilidade dos serviços externos no runner. A ativação de uma API também precisará de uma execução real de validação.
