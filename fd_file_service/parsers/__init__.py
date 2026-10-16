"""解析器实现（任务 5.2 / 5.3）：把源文件變成纯文本。

为什么单独一层这个包，而不是直接在 ``parse.py`` 里写
----------------------------------------------------
``parse.py`` 负责**编排**（取字节 → 调用 → 存产物 → 失败处置），这里负责
**怎么把某种格式变成文本**。两者变更原因不同：编排随运维关注点变（重试、超时、
产物存储），解析器随格式与引擎变。v2 加 OCR（Tesseract）就是新增一个模块，
不需要改 ``parse.py``。

## 一个必须写明的发现：Gotenberg **没有**文本抽取端点

核对 Gotenberg 源码（upstream ``main``）确认，它只产出 PDF，不会吐出文本：

- ``pkg/modules/libreoffice/routes.go``：唯一路由 ``/forms/libreoffice/convert``，
  输出路径写死为 ``ctx.GeneratePath(".pdf")``；
- ``pkg/modules/pdfengines/routes.go``：全部 16 条路由（merge / split / flatten /
  optimize / convert / metadata / bookmarks / encrypt / embed / watermark /
  stamp / rotate / factur-x）均以 PDF 为输入、**PDF 为输出**——没有任何
  extract-text；
- 外界流传的 ``/forms/pdfengines/extract`` 类端点在当前版本并不存在。

因此 v1 的「PDF/Office → 文本」实际是一条**两段管线**：

    Office (docx/xlsx/pptx) --Gotenberg--> PDF --本地抽取器--> 文本
    PDF ---------------------------------> PDF --本地抽取器--> 文本

第一段必须用 Gotenberg（LibreOffice 是唯一能读几十种 Office 方言的东西，自研
不现实）；PDF→文本只是标准解析，本地库即可完事，不必再绕一次 HTTP 和外部服务。

这也带来一个额外的好处：**Gotenberg 挂掉时，原生 PDF 仍能完成文本抽取**——
可检索性不会整体降至零，只有 Office 文件受影响。
"""
