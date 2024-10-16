import React, { useState, ReactNode, useCallback, useMemo, memo } from "react";
import { FiCheck, FiCopy } from "react-icons/fi";

const CODE_BLOCK_PADDING_TYPE = { padding: "1rem" };

interface CodeBlockProps {
  className?: string | undefined;
  children?: ReactNode;
  content: string;
  [key: string]: any;
}

export const CodeBlock = memo(function CodeBlock({
  className = "",
  children,
  content,
  ...props
}: CodeBlockProps) {
  const [copied, setCopied] = useState(false);

  const language = useMemo(() => {
    return className
      .split(" ")
      .filter((cls) => cls.startsWith("language-"))
      .map((cls) => cls.replace("language-", ""))
      .join(" ");
  }, [className]);

  const codeText = useMemo(() => {
    let codeText: string | null = null;
    if (
      props.node?.position?.start?.offset &&
      props.node?.position?.end?.offset
    ) {
      codeText = content.slice(
        props.node.position.start.offset,
        props.node.position.end.offset
      );
      codeText = codeText.trim();

      // Find the last occurrence of closing backticks
      const lastBackticksIndex = codeText.lastIndexOf("```");
      if (lastBackticksIndex !== -1) {
        codeText = codeText.slice(0, lastBackticksIndex + 3);
      }

      // Remove the language declaration and trailing backticks
      const codeLines = codeText.split("\n");
      if (
        codeLines.length > 1 &&
        (codeLines[0].startsWith("```") ||
          codeLines[0].trim().startsWith("```"))
      ) {
        codeLines.shift(); // Remove the first line with the language declaration
        if (
          codeLines[codeLines.length - 1] === "```" ||
          codeLines[codeLines.length - 1]?.trim() === "```"
        ) {
          codeLines.pop(); // Remove the last line with the trailing backticks
        }

        const minIndent = codeLines
          .filter((line) => line.trim().length > 0)
          .reduce((min, line) => {
            const match = line.match(/^\s*/);
            return Math.min(min, match ? match[0].length : 0);
          }, Infinity);

        const formattedCodeLines = codeLines.map((line) =>
          line.slice(minIndent)
        );
        codeText = formattedCodeLines.join("\n");
      }
    }

    // handle unknown languages. They won't have a `node.position.start.offset`
    if (!codeText) {
      const findTextNode = (node: any): string | null => {
        if (node.type === "text") {
          return node.value;
        }
        let finalResult = "";
        if (node.children) {
          for (const child of node.children) {
            const result = findTextNode(child);
            if (result) {
              finalResult += result;
            }
          }
        }
        return finalResult;
      };

      codeText = findTextNode(props.node);
    }

    return codeText;
  }, [content, props.node]);

  const handleCopy = useCallback(
    (event: React.MouseEvent) => {
      event.preventDefault();
      if (!codeText) {
        return;
      }

      navigator.clipboard.writeText(codeText).then(() => {
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
      });
    },
    [codeText]
  );

  if (!language) {
    if (typeof children === "string") {
      return <code className={className}>{children}</code>;
    }

    return (
      <pre style={CODE_BLOCK_PADDING_TYPE}>
        <code {...props} className={`text-sm ${className}`}>
          {children}
        </code>
      </pre>
    );
  }

  return (
    <div className="overflow-x-hidden">
      <div className="flex mx-3 py-2 text-xs">
        {language}
        {codeText && (
          <div
            className="ml-auto cursor-pointer select-none"
            onMouseDown={handleCopy}
          >
            {copied ? (
              <div className="flex items-center space-x-2">
                <FiCheck size={16} />
                <span>Copied!</span>
              </div>
            ) : (
              <div className="flex items-center space-x-2">
                <FiCopy size={16} />
                <span>Copy code</span>
              </div>
            )}
          </div>
        )}
      </div>
      <pre {...props} className="overflow-x-scroll" style={{ padding: "1rem" }}>
        <code className={`text-xs overflow-x-auto `}>{children}</code>
      </pre>
    </div>
  );
});
