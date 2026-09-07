// Inspect the compiler's public declaration, never execute SDK implementation.
import ts from "typescript";
import { fileURLToPath } from "node:url";
const source = fileURLToPath(new URL("../src/index.ts", import.meta.url));
const config = ts.readConfigFile(fileURLToPath(new URL("../tsconfig.json", import.meta.url)), ts.sys.readFile);
const options = ts.parseJsonConfigFileContent(config.config, ts.sys, fileURLToPath(new URL("..", import.meta.url))).options;
const program = ts.createProgram([source], { ...options, declaration: true, emitDeclarationOnly: true, removeComments: true });
let declaration;
const result = program.emit(undefined, (path, text) => {
  if (path.endsWith("/src/index.d.ts")) declaration = text;
});
const diagnostics = [...ts.getPreEmitDiagnostics(program), ...result.diagnostics];
if (diagnostics.length || !declaration) throw new Error(ts.formatDiagnosticsWithColorAndContext(diagnostics, {
  getCurrentDirectory: ts.sys.getCurrentDirectory, getCanonicalFileName: p => p, getNewLine: () => "\n",
}));
const ast = ts.createSourceFile("index.d.ts", declaration, ts.ScriptTarget.Latest, true);
const client = ast.statements.find(n => ts.isClassDeclaration(n) && n.name?.text === "AsyncIrisMemoryClient");
const methods = client.members.filter(ts.isMethodDeclaration).map(n => n.name.getText(ast));
const exports = ast.statements.filter(n => n.modifiers?.some(m => m.kind === ts.SyntaxKind.ExportKeyword)).map(n => n.name.text);
process.stdout.write(JSON.stringify({ declaration, methods: methods.sort(), exports: exports.sort() }));
