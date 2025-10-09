type Props = { params: { address: string } };

export default function PoolDetailPage({ params }: Props) {
  const { address } = params;
  return (
    <main style={{ padding: 24 }}>
      <h1>Pool Detail</h1>
      <p>Address: {address}</p>
      <div>Charts and metrics replicated from Streamlit will render here.</div>
    </main>
  );
}


