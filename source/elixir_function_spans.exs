defmodule FunctionSpans do
  @defs [:def, :defp, :defmacro, :defmacrop, :defguard, :defguardp]
  @attachments [:doc, :spec, :impl]

  def run(path) do
    source = File.read!(path) |> String.trim_leading("\uFEFF")

    case Code.string_to_quoted(source, columns: true, token_metadata: true, file: path) do
      {:ok, ast} ->
        functions = ast |> collect_functions() |> Enum.sort()
        attributes = ast |> collect_attributes()
        functions
        |> attach(source, attributes)
        |> Enum.each(fn {first, last} -> IO.puts("#{first}\t#{last}") end)

      {:error, {location, message, token}} ->
        IO.puts(:stderr, "#{inspect(location)}: #{message} #{inspect(token)}")
        System.halt(2)
    end
  end

  defp collect_functions(ast) do
    {_, spans} = Macro.prewalk(ast, [], fn
      {name, meta, args} = node, acc when name in @defs and is_list(args) ->
        {node, [{Keyword.fetch!(meta, :line), end_line(node, meta)} | acc]}
      node, acc -> {node, acc}
    end)
    spans
  end

  defp collect_attributes(ast) do
    {_, attrs} = Macro.prewalk(ast, [], fn
      {:@, meta, [{name, _, _}]} = node, acc when name in @attachments ->
        {node, [{Keyword.fetch!(meta, :line), end_line(node, meta)} | acc]}
      node, acc -> {node, acc}
    end)
    attrs
  end

  defp end_line({_, meta, args}, fallback) do
    case Keyword.get(meta, :end) do
      nil -> max_line(args, Keyword.fetch!(fallback, :line))
      ending when is_list(ending) -> Keyword.get(ending, :line, Keyword.fetch!(fallback, :line))
    end
  end

  defp max_line(term, current) when is_list(term), do: Enum.reduce(term, current, &max_line/2)
  defp max_line({_, meta, args}, current) when is_list(meta) do
    current = max(current, Keyword.get(meta, :line, current))
    current = case Keyword.get(meta, :end) do
      ending when is_list(ending) -> max(current, Keyword.get(ending, :line, current))
      _ -> current
    end
    max_line(args, current)
  end
  defp max_line(_, current), do: current

  defp attach(spans, source, attributes) do
    lines = String.split(source, ~r/\R/, trim: false)
    Enum.map(spans, fn {first, last} -> {attachment_start(lines, first, attributes), last} end)
  end

  defp attachment_start(lines, first, attributes) do
    {cursor, best} = comments_before(lines, first - 1, first)
    case Enum.find(attributes, fn {_start, ending} -> ending == cursor end) do
      {attr_start, _} -> attachment_start(lines, attr_start, attributes)
      nil -> best
    end
  end

  defp comments_before(_lines, cursor, best) when cursor < 1, do: {cursor, best}
  defp comments_before(lines, cursor, best) do
    line = lines |> Enum.at(cursor - 1) |> String.trim()
    if String.starts_with?(line, "#") do
      comments_before(lines, cursor - 1, cursor)
    else
      {cursor, best}
    end
  end
end

case System.argv() do
  [path] -> FunctionSpans.run(path)
  _ -> IO.puts(:stderr, "usage: elixir elixir_function_spans.exs SOURCE"); System.halt(2)
end
