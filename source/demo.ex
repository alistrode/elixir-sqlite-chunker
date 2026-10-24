defmodule OwnershipDemo do
  @doc "Return the value when it is enabled."
  @spec enabled?(boolean()) :: boolean()
  def enabled?(value) do
    if value do
      true
    else
      false
    end
  end

  # A single-line private function.
  defp label(value), do: "value: #{value}"
end
